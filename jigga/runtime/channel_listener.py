"""Channel listener — efficient inbound for channels via long-polling.

Replaces the cron-cadence idea (polling every few seconds is wasteful and
laggy) with a long-lived listener loop. Each cycle long-polls every enabled
channel — the HTTP call blocks server-side up to ~30s until a message arrives
or the timeout elapses — turns inbound messages into tasks, then wakes the
assigned agent (PR C's tool-use loop) so it can act/reply.

Run it as its own process alongside the supervisor:

    jigga channels listen

## Pipeline (per cycle, per enabled channel)

    long-poll → normalized messages
      → create one task per message (assignee = channel's default_agent,
        description carries the text + chat_id + a reply hint)
      → emit `channel.message.received`
      → run_agent once per affected assignee (so the agent can reply via
        <channel>.send_message if it's tool-configured + autonomous)

## Generality

Channels go through the normalized gateway (`runtime.channels`): each implements
the `ChannelAdapter` contract and produces `JiggaEvent`s, and every inbound
message passes the `identity_allowed` policy/identity check before becoming a
task. Adding Slack / iMessage = register an adapter in `channels.ADAPTERS`; the
listener is unchanged. Enabled channels come from `config.channels.<name>.enabled`.

## Known limitation (follow-up)

Channels are polled sequentially, so with N channels the worst-case latency is
N × long_poll_seconds. Fine for one channel; multi-channel wants a thread per
channel. Documented in docs/CHANNELS_TELEGRAM_RUNTIME_NOTES.md.
"""

from __future__ import annotations

import signal
import time
from pathlib import Path
from typing import Any

from jigga.core.config import load_runtime_config
from jigga.runtime.agent import run_agent
from jigga.runtime.audit import append_event, trace_context
from jigga.runtime.channels import ADAPTERS, JiggaEvent, identity_allowed
from jigga.runtime.tasks import create_task

DEFAULT_LONG_POLL_SECONDS = 30


def enabled_channels(home: Path) -> list[tuple[str, dict[str, Any]]]:
    """Return [(channel_name, config)] for channels with enabled=true that also
    have a registered adapter in the gateway."""
    config = load_runtime_config(home)
    channels = config.get("channels") or {}
    result: list[tuple[str, dict[str, Any]]] = []
    for name, cfg in channels.items():
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            continue
        if name not in ADAPTERS:
            continue
        result.append((name, cfg))
    return result


def _event_to_task_fields(event: JiggaEvent) -> tuple[str, str]:
    sender = event.actor_name
    chat_id = event.conversation_id
    title = f"{event.source} message from {sender}"
    description = (
        f"Message received via {event.source} from {sender} (chat_id: {chat_id}):\n\n"
        f"{event.text}\n\n"
        f"To reply, use the {event.source}.send_message capability with chat_id={chat_id}."
    )
    return title, description


def ingest_once(
    home: Path,
    logs_dir: Path,
    tasks_dir: Path,
    agents_dir: Path,
    *,
    long_poll_seconds: int = DEFAULT_LONG_POLL_SECONDS,
    process_agents: bool = True,
) -> dict[str, Any]:
    """Run a single ingest cycle across all enabled channels. Returns a summary.

    Factored out of the loop so tests can drive exactly one cycle and the loop
    stays a thin wrapper. One trace per cycle ties the inbound messages, the
    tasks they create, and the agent runs they trigger together.
    """
    with trace_context():
        return _ingest_once(
            home, logs_dir, tasks_dir, agents_dir,
            long_poll_seconds=long_poll_seconds, process_agents=process_agents,
        )


def _ingest_once(
    home: Path,
    logs_dir: Path,
    tasks_dir: Path,
    agents_dir: Path,
    *,
    long_poll_seconds: int = DEFAULT_LONG_POLL_SECONDS,
    process_agents: bool = True,
) -> dict[str, Any]:
    created: list[dict[str, Any]] = []
    affected_agents: set[str] = set()
    polled: list[str] = []

    for name, cfg in enabled_channels(home):
        adapter = ADAPTERS[name]
        polled.append(name)
        result = adapter.poll(home, long_poll_seconds=long_poll_seconds)
        if result.get("status") and result["status"] != "ok":
            append_event(logs_dir, "channel.poll_skipped", status="ask", channel=name,
                         detail=result.get("status"))
            continue
        default_agent = cfg.get("default_agent")
        for event in result.get("events", []):
            # Policy / identity check — the gateway authorizes every sender.
            if not identity_allowed(event, cfg):
                append_event(logs_dir, "channel.message.rejected", status="deny", channel=name,
                             chat_id=event.conversation_id, sender=event.actor_name,
                             event_id=event.id, reason="sender not in allowlist")
                continue
            event.target = {"agent": default_agent}
            title, description = _event_to_task_fields(event)
            task = create_task(
                tasks_dir,
                title=title,
                description=description,
                assignee=default_agent,
                metadata={
                    "channel": name,
                    "chat_id": event.conversation_id,
                    "sender": event.actor.get("name"),
                    "message_id": event.raw.get("message_id"),
                    "text": event.text,
                    "event_id": event.id,
                },
            )
            created.append(task.to_dict())
            append_event(logs_dir, "channel.message.received", channel=name, task_id=task.id,
                         chat_id=event.conversation_id, sender=event.actor.get("name"), event_id=event.id)
            if default_agent:
                affected_agents.add(default_agent)

    runs: list[dict[str, Any]] = []
    if process_agents:
        for agent_id in sorted(affected_agents):
            runs.append(run_agent(home, logs_dir, tasks_dir, agents_dir, agent_id))

    return {"polled": polled, "created": created, "runs": runs}


def channel_listen(
    home: Path,
    logs_dir: Path,
    tasks_dir: Path,
    agents_dir: Path,
    *,
    long_poll_seconds: int = DEFAULT_LONG_POLL_SECONDS,
    max_cycles: int | None = None,
    process_agents: bool = True,
) -> dict[str, Any]:
    """Long-poll enabled channels until interrupted (or `max_cycles` reached,
    for tests/bounded runs). Graceful SIGINT/SIGTERM shutdown."""
    stopped = {"flag": False, "signal": None}

    def _handle(signum: int, _frame: Any) -> None:
        stopped["flag"] = True
        stopped["signal"] = signum

    previous_int = previous_term = None
    try:
        previous_int = signal.signal(signal.SIGINT, _handle)
        previous_term = signal.signal(signal.SIGTERM, _handle)
    except (ValueError, OSError):
        pass

    append_event(logs_dir, "channel.listen.started", long_poll_seconds=long_poll_seconds,
                 max_cycles=max_cycles, channels=[n for n, _ in enabled_channels(home)])

    cycles = 0
    cycle_summaries: list[dict[str, Any]] = []
    try:
        while not stopped["flag"] and (max_cycles is None or cycles < max_cycles):
            summary = ingest_once(
                home, logs_dir, tasks_dir, agents_dir,
                long_poll_seconds=long_poll_seconds, process_agents=process_agents,
            )
            cycle_summaries.append(summary)
            cycles += 1
            if stopped["flag"] or (max_cycles is not None and cycles >= max_cycles):
                break
            # No messages this cycle? brief pause so a non-long-poll channel (or
            # an empty long-poll) doesn't hot-loop. Long-poll already blocks, so
            # this is mostly a safety floor.
            if not summary["created"]:
                time.sleep(0.5 if long_poll_seconds <= 0 else 0)
    finally:
        if previous_int is not None:
            try:
                signal.signal(signal.SIGINT, previous_int)
            except (ValueError, OSError):
                pass
        if previous_term is not None:
            try:
                signal.signal(signal.SIGTERM, previous_term)
            except (ValueError, OSError):
                pass

    status = "interrupted" if stopped["flag"] else "stopped"
    append_event(logs_dir, "channel.listen.stopped", status="ok", cycles=cycles,
                 stopped_by_signal=stopped["signal"])
    return {"status": status, "cycles": cycles, "stopped_by_signal": stopped["signal"],
            "summaries": cycle_summaries}

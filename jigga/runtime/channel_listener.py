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

`CHANNEL_POLLERS` maps a channel name to its poll function. Today only
`telegram` is registered; Slack / iMessage register here once they exist and
inherit the whole listener for free. Enabled channels come from
`config.channels.<name>.enabled`.

## Known limitation (follow-up)

Channels are polled sequentially, so with N channels the worst-case latency is
N × long_poll_seconds. Fine for one channel; multi-channel wants a thread per
channel. Documented in docs/CHANNELS_TELEGRAM_RUNTIME_NOTES.md.
"""

from __future__ import annotations

import signal
import time
from pathlib import Path
from typing import Any, Callable

from jigga.core.config import load_runtime_config
from jigga.runtime import telegram
from jigga.runtime.agent import run_agent
from jigga.runtime.audit import append_event
from jigga.runtime.tasks import create_task

def _poll_telegram(home: Path, **kwargs: Any) -> dict[str, Any]:
    # Thin wrapper so the registry resolves telegram.poll_messages at CALL time
    # (not import time) — keeps it patchable in tests and decoupled from the
    # concrete function object.
    return telegram.poll_messages(home, **kwargs)


# channel name -> poll function (home, *, long_poll_seconds) -> {messages: [...]}
CHANNEL_POLLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "telegram": _poll_telegram,
}

DEFAULT_LONG_POLL_SECONDS = 30


def enabled_channels(home: Path) -> list[tuple[str, dict[str, Any]]]:
    """Return [(channel_name, config)] for channels with enabled=true that also
    have a registered poller."""
    config = load_runtime_config(home)
    channels = config.get("channels") or {}
    result: list[tuple[str, dict[str, Any]]] = []
    for name, cfg in channels.items():
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            continue
        if name not in CHANNEL_POLLERS:
            continue
        result.append((name, cfg))
    return result


def _message_to_task_fields(channel: str, message: dict[str, Any]) -> tuple[str, str]:
    sender = message.get("sender") or "unknown"
    chat_id = message.get("chat_id")
    text = message.get("text") or ""
    title = f"{channel} message from {sender}"
    description = (
        f"Message received via {channel} from {sender} (chat_id: {chat_id}):\n\n"
        f"{text}\n\n"
        f"To reply, use the {channel}.send_message capability with chat_id={chat_id}."
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
    stays a thin wrapper.
    """
    created: list[dict[str, Any]] = []
    affected_agents: set[str] = set()
    polled: list[str] = []

    for name, cfg in enabled_channels(home):
        poller = CHANNEL_POLLERS[name]
        polled.append(name)
        result = poller(home, long_poll_seconds=long_poll_seconds)
        if result.get("status") and result["status"] != "ok":
            append_event(logs_dir, "channel.poll_skipped", status="ask", channel=name,
                         detail=result.get("status"))
            continue
        default_agent = cfg.get("default_agent")
        for message in result.get("messages", []):
            title, description = _message_to_task_fields(name, message)
            task = create_task(
                tasks_dir,
                title=title,
                description=description,
                assignee=default_agent,
                metadata={
                    "channel": name,
                    "chat_id": message.get("chat_id"),
                    "sender": message.get("sender"),
                    "message_id": message.get("message_id"),
                    "text": message.get("text"),
                },
            )
            created.append(task.to_dict())
            append_event(logs_dir, "channel.message.received", channel=name, task_id=task.id,
                         chat_id=message.get("chat_id"), sender=message.get("sender"))
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

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

from jigga.core.config import load_runtime_config, resolve_default_agent
from jigga.runtime.agent import run_agent
from jigga.runtime.audit import ACTOR_USER, actor_context, append_event, trace_context
from jigga.runtime.approvals import find_by_code, parse_approval_reply, resolve_and_requeue
from jigga.runtime.channels import (
    ADAPTERS,
    JiggaEvent,
    activation_allows,
    from_public_conversation,
    identity_allowed,
)
from jigga.runtime.tasks import create_task, find_task

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


def long_polling_channels_enabled(home: Path) -> bool:
    """Is any enabled channel's adapter a real long-poller (blocks on
    `long_poll_seconds`)? The supervisor loop paces on this: a blocking poll
    lets ticks run back-to-back; instant-return channels (webchat's local file)
    must not drop the inter-tick sleep or the loop hot-spins."""
    return any(getattr(ADAPTERS[name], "long_polls", False)
               for name, _ in enabled_channels(home))


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
    only_channel: str | None = None,
) -> dict[str, Any]:
    """Run a single ingest cycle across all enabled channels. Returns a summary.

    Factored out of the loop so tests can drive exactly one cycle and the loop
    stays a thin wrapper. One trace per cycle ties the inbound messages, the
    tasks they create, and the agent runs they trigger together.

    `only_channel` scopes the cycle to a single enabled channel — the
    `webchat send --wait` path uses it so a synchronous browser send never
    blocks behind another channel's long-poll.
    """
    with trace_context():
        return _ingest_once(
            home, logs_dir, tasks_dir, agents_dir,
            long_poll_seconds=long_poll_seconds, process_agents=process_agents,
            only_channel=only_channel,
        )


def _ingest_once(
    home: Path,
    logs_dir: Path,
    tasks_dir: Path,
    agents_dir: Path,
    *,
    long_poll_seconds: int = DEFAULT_LONG_POLL_SECONDS,
    process_agents: bool = True,
    only_channel: str | None = None,
) -> dict[str, Any]:
    created: list[dict[str, Any]] = []
    affected_agents: set[str] = set()
    polled: list[str] = []

    for name, cfg in enabled_channels(home):
        if only_channel is not None and name != only_channel:
            continue
        adapter = ADAPTERS[name]
        polled.append(name)
        # The poll is a network WAIT (Telegram long-polls for 30s), not
        # execution — deliberately outside the lock, or the runtime would look
        # busy to everyone else for the whole time it sat listening.
        result = adapter.poll(home, long_poll_seconds=long_poll_seconds)
        if result.get("status") and result["status"] != "ok":
            append_event(logs_dir, "channel.poll_skipped", status="ask", channel=name,
                         detail=result.get("status"))
            continue
        # The channel's configured target, else the global default/chief agent
        # (catch-all for unrouted inbound), else nothing.
        default_agent = cfg.get("default_agent") or resolve_default_agent(home / "agents")
        for event in result.get("events", []):
            # Policy / identity check — the gateway authorizes every sender.
            if not identity_allowed(event, cfg):
                append_event(logs_dir, "channel.message.rejected", status="deny", channel=name,
                             chat_id=event.conversation_id, sender=event.actor_name,
                             event_id=event.id, reason="sender not in allowlist")
                continue
            # Everything past the allowlist check is an authorized person
            # acting: the task they create, the approval they resolve. The
            # precursor stack could not tell those apart from automation
            # writing through the same API.
            with actor_context(f"{ACTOR_USER}:{name}"):
                # Approval reply? (B6) An allowlisted sender typing `approve <code>` /
                # `deny <code>` resolves a pending approval and re-queues the held
                # task — it is not turned into a new task. Only intercept when the
                # code matches a real pending approval (so ordinary prose passes through).
                parsed = parse_approval_reply(event.text)
                if parsed is not None:
                    approved, code = parsed
                    record = find_by_code(home / "approvals", code)
                    if record is not None:
                        resolve_and_requeue(home / "approvals", tasks_dir, code, approved=approved)
                        append_event(logs_dir, "approval.resolved", status="ok" if approved else "deny",
                                     channel=name, code=code, approved=approved,
                                     task_id=record.get("task_id"), agent_id=record.get("agent_id"))
                        if approved and record.get("agent_id"):
                            affected_agents.add(record["agent_id"])
                        try:
                            adapter.send(home, conversation_id=event.conversation_id,
                                         text=f"{'Approved' if approved else 'Denied'} {code}."
                                              + (" Resuming." if approved else ""))
                        except Exception as exc:  # noqa: BLE001 — notify failure must not break ingest
                            append_event(logs_dir, "approval.notify_failed", status="error", code=code, error=str(exc))
                        continue
                    # no matching pending code → fall through; treat as a normal message

                # Activation mode — should this message wake the agent at all?
                if not activation_allows(event, cfg):
                    append_event(logs_dir, "channel.message.ignored", channel=name,
                                 chat_id=event.conversation_id, event_id=event.id,
                                 reason=f"activation={cfg.get('activation') or 'always'}",
                                 conversation_type=event.conversation_type)
                    continue
                # An adapter may pre-target a specific agent (webchat's agent
                # picker sets event.target from the sender's choice). Honor it only
                # when that agent actually exists — else audit and fall back to the
                # channel default, so a typo'd/stale target can't drop a message.
                assignee = default_agent
                requested = (event.target or {}).get("agent")
                if requested and requested != default_agent:
                    if (Path(agents_dir) / f"{requested}.yaml").exists():
                        assignee = requested
                    else:
                        append_event(logs_dir, "channel.target_unknown", status="ask", channel=name,
                                     requested_agent=requested, fallback=default_agent,
                                     event_id=event.id)
                event.target = {"agent": assignee}
                # Conversation continuity for external channels: record every
                # gate-passing inbound message into the channel's local transcript
                # (webchat declares `self_transcribed` — its inbox IS the
                # transcript). Best-effort: a transcript fault must not break
                # ingest; the message still becomes a task either way.
                if not getattr(adapter, "self_transcribed", False):
                    try:
                        from jigga.runtime.channel_transcript import record as record_transcript
                        record_transcript(home, name, conversation_id=event.conversation_id,
                                          sender=event.actor_name, text=event.text,
                                          direction="in", message_id=event.raw.get("message_id"))
                    except Exception as exc:  # noqa: BLE001
                        append_event(logs_dir, "channel.transcript_error", status="error",
                                     channel=name, error=str(exc))
                title, description = _event_to_task_fields(event)
                task = create_task(
                    tasks_dir,
                    title=title,
                    description=description,
                    assignee=assignee,
                    metadata={
                        "channel": name,
                        "chat_id": event.conversation_id,
                        "sender": event.actor.get("name"),
                        "message_id": event.raw.get("message_id"),
                        "text": event.text,
                        "event_id": event.id,
                        "conversation_type": event.conversation_type,
                        # Group/channel messages should run with restricted memory
                        # (prompt-injection safety). Recorded now; agent-loop
                        # enforcement of the restricted scope is a follow-up.
                        "restricted_memory": from_public_conversation(event),
                    },
                )
                created.append(task.to_dict())
                append_event(logs_dir, "channel.message.received", channel=name, task_id=task.id,
                             chat_id=event.conversation_id, sender=event.actor.get("name"), event_id=event.id)
                if assignee:
                    affected_agents.add(assignee)

    runs: list[dict[str, Any]] = []
    if process_agents and affected_agents:
        from jigga.runtime.disabled import disabled_agent_ids
        from jigga.runtime.execution_lock import execution_lock

        # Running agents is the part that must not overlap another process:
        # claiming a task is a read-modify-write, so two runners would both
        # take it. Non-blocking, and skipping loses nothing — the messages are
        # already durable TASKS by this point, so whoever holds the lock (or
        # the next tick) runs them. Blocking here would instead let one wedged
        # holder stall the supervisor indefinitely.
        with execution_lock(home) as acquired:
            if not acquired:
                append_event(logs_dir, "channel.run_deferred", status="ask",
                             agents=sorted(affected_agents),
                             reason="another process is running agents for this home")
                return {"polled": polled, "created": created, "runs": runs}
            disabled = disabled_agent_ids(home, home / "teams")
            for agent_id in sorted(affected_agents):
                if agent_id in disabled:
                    append_event(logs_dir, "channel.agent_disabled", status="ask", agent=agent_id)
                    continue
                runs.append(run_agent(home, logs_dir, tasks_dir, agents_dir, agent_id))
            _notify_failed_channel_tasks(home, logs_dir, tasks_dir, created)

    return {"polled": polled, "created": created, "runs": runs}


_FAILURE_REPLY = ("⚠️ I couldn't finish that just now (often a temporary rate limit). "
                  "Please try again in a moment.")


def _notify_failed_channel_tasks(
    home: Path, logs_dir: Path, tasks_dir: Path, created: list[dict[str, Any]]
) -> None:
    """After running channel-created tasks, send a short error reply for any that
    ended up `failed` (e.g. the model was rate-limited), so the user gets
    feedback instead of silence. The reply goes out via the channel adapter
    directly (the bot's own token), so it isn't subject to the agent's tool/
    network policy. Best-effort: a notify failure must not break ingest."""
    for created_task in created:
        meta = created_task.get("metadata") or {}
        channel = meta.get("channel")
        chat_id = meta.get("chat_id")
        if not channel or chat_id is None or channel not in ADAPTERS:
            continue
        task = find_task(tasks_dir, created_task.get("id"))
        if task is None or task.state != "failed":
            continue
        try:
            ADAPTERS[channel].send(home, conversation_id=chat_id, text=_FAILURE_REPLY)
            append_event(logs_dir, "channel.failure_notified", channel=channel,
                         task_id=task.id, chat_id=chat_id)
        except Exception as exc:  # noqa: BLE001 — notify failure must not break ingest
            append_event(logs_dir, "channel.failure_notify_error", status="error",
                         task_id=created_task.get("id"), error=str(exc))


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

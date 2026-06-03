from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_agents, load_workflows, max_wakes_per_hour
from jigga.core.models import now_iso
from jigga.core.paths import get_paths
from jigga.runtime.agent import run_agent
from jigga.runtime.audit import append_event, trace_context
from jigga.runtime.channel_listener import enabled_channels, ingest_once
from jigga.runtime.compaction import maybe_compact
from jigga.runtime.log_rotation import rotate_logs
from jigga.runtime.loop_guard import (
    cron_already_fired,
    load_loop_state,
    now_utc,
    record_cron_fire,
    record_wake,
    save_loop_state,
    should_skip_wake,
)
from jigga.runtime.state import read_state, write_state
from jigga.runtime.scheduler import due_events
from jigga.runtime.tasks import create_task, pending_summary
from jigga.runtime.workflow import run_workflow


def _poll_channels(paths: Any, long_poll_seconds: int = 0) -> None:
    """Poll enabled channels into tasks on the heartbeat (B2) and run the agent
    on them immediately (`process_agents=True`). No-op when no channel is
    enabled. Errors are contained so a flaky network or channel can't take the
    supervisor down.

    Channel messages are *user-initiated*, so they run right here rather than
    via the tick's agent-waking loop below — that loop applies the
    `max_wakes_per_hour` throttle, which exists to stop runaway *autonomous*
    (cron/self) wake loops and must NOT rate-limit a person typing in chat.
    Running here matches `jigga channels listen` and keeps replies prompt.

    `long_poll_seconds` is the Telegram long-poll timeout: 0 = a single
    non-blocking poll (the legacy tick behavior); >0 makes the call block until
    a message arrives or the timeout elapses, which is what lets the supervisor
    loop run channels in near-real-time instead of once per cron interval."""
    if not enabled_channels(paths.home):
        return
    try:
        ingest_once(paths.home, paths.logs, paths.tasks, paths.agents,
                    long_poll_seconds=long_poll_seconds, process_agents=True)
    except Exception as exc:  # noqa: BLE001 — the supervisor must survive any channel fault
        append_event(paths.logs, "channel.ingest_error", status="error", error=str(exc))


def supervisor_tick(home: str | Path | None = None, *, channel_long_poll_seconds: int = 0) -> dict[str, Any]:
    # One trace per tick: every event this tick produces — agent runs, workflow
    # runs, and the subagents they spawn — shares this id, so `jigga trace <id>`
    # returns the whole tick's causal tree.
    with trace_context():
        return _supervisor_tick(home, channel_long_poll_seconds=channel_long_poll_seconds)


def _supervisor_tick(home: str | Path | None = None, *, channel_long_poll_seconds: int = 0) -> dict[str, Any]:
    paths = get_paths(home)
    # Roll the audit log over (by day / size) and prune old archives on the
    # heartbeat, so the write path stays free of this. Emits an event when it
    # actually rotates or prunes.
    rotation = rotate_logs(paths.home, paths.logs)
    if rotation["rotated"] or rotation["pruned"]:
        append_event(paths.logs, "logs.rotated", archived=rotation["rotated"],
                     pruned=rotation["pruned"])
    # Compact memory at most once/day (D3) so it stays bounded — archive old raw
    # entries, stale team facts, and finished tasks. Contained so a fault can't
    # break the tick.
    try:
        compacted = maybe_compact(paths.home)
        if compacted:
            append_event(paths.logs, "memory.compacted", raw_archived=len(compacted["raw_archived"]),
                         facts_archived=compacted["facts_archived"], tasks_archived=len(compacted["tasks_archived"]))
    except Exception as exc:  # noqa: BLE001 — compaction must not break the tick
        append_event(paths.logs, "memory.compact_error", status="error", error=str(exc))
    # Channels poll on the heartbeat (B2): enabled bots always respond whenever
    # the supervisor runs — no separate `jigga channels listen` needed. We only
    # create tasks here; the tick's own agent-waking loop below runs them, so
    # there's one execution path. Any channel/network error is contained so it
    # can't break the tick. `channel_long_poll_seconds` defaults to 0 (a single
    # non-blocking poll), but the supervisor loop passes a real long-poll timeout
    # when a channel is enabled, so a message is picked up the instant it arrives
    # rather than waiting for the next cron interval (near-real-time chat).
    _poll_channels(paths, long_poll_seconds=channel_long_poll_seconds)
    agents = load_agents(paths.agents)
    workflows = load_workflows(paths.workflows)
    events = due_events(paths.agents, paths.workflows, agents=agents, workflows=workflows)
    loop_state = load_loop_state(paths.home)
    now = now_utc()
    wake_limit = max_wakes_per_hour(paths.home)
    deduped_events: list[dict[str, Any]] = []
    skipped_events: list[dict[str, Any]] = []

    for event in events:
        event_dict = event.to_dict()
        if event.type == "cron.tick":
            cron = event.payload.get("cron", "")
            target = event.targets[0] if event.targets else ""
            if cron_already_fired(loop_state, target, cron, now):
                skipped_events.append({"reason": "cron.deduplicated", "event": event_dict})
                append_event(paths.logs, "supervisor.cron_deduplicated", agent=target, cron=cron)
                continue
            record_cron_fire(loop_state, target, cron, now)
            append_event(paths.logs, "event.created", **event_dict)
            for agent_id in event.targets:
                create_task(
                    paths.tasks,
                    title=f"Scheduled wake: {event.payload.get('schedule', event.id)}",
                    # A recipe cronJob's `message` (the work-loop instruction)
                    # becomes the task body so the agent knows what to do.
                    description=event.payload.get("message"),
                    assignee=agent_id,
                    metadata={"event": event_dict},
                )
            deduped_events.append(event_dict)
        elif event.type == "workflow.schedule_due":
            workflow_id = event.payload.get("workflow")
            cron_key = f"workflow:{workflow_id}"
            if cron_already_fired(loop_state, cron_key, event.payload.get("schedule", ""), now):
                skipped_events.append({"reason": "workflow.deduplicated", "event": event_dict})
                continue
            record_cron_fire(loop_state, cron_key, event.payload.get("schedule", ""), now)
            append_event(paths.logs, "event.created", **event_dict)
            if workflow_id in workflows:
                run_workflow(paths, workflow_id)
            deduped_events.append(event_dict)
        else:
            append_event(paths.logs, "event.created", **event_dict)
            deduped_events.append(event_dict)

    targets, pending_task_count = pending_summary(paths.tasks)
    append_event(
        paths.logs,
        "supervisor.tick",
        targets=targets,
        pending_task_count=pending_task_count,
        event_count=len(deduped_events),
        skipped_event_count=len(skipped_events),
    )

    runs = []
    throttled: list[str] = []
    for agent_id in targets:
        if agent_id not in agents:
            append_event(paths.logs, "supervisor.target_missing", status="failed", agent=agent_id)
            continue
        if should_skip_wake(loop_state, agent_id, wake_limit, now):
            throttled.append(agent_id)
            append_event(
                paths.logs,
                "supervisor.wake_throttled",
                status="ask",
                agent=agent_id,
                limit_per_hour=wake_limit,
            )
            continue
        record_wake(loop_state, agent_id, now)
        runs.append(run_agent(paths.home, paths.logs, paths.tasks, paths.agents, agent_id))

    save_loop_state(paths.home, loop_state)

    state = read_state(paths.state)
    state.last_supervisor_tick_at = now_iso()
    write_state(paths.state, state)
    return {
        "events": deduped_events,
        "skipped_events": skipped_events,
        "targets": targets,
        "throttled": throttled,
        "runs": runs,
    }

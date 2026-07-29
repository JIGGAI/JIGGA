from __future__ import annotations

import time
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


class _Backoff:
    """Exponential backoff for a repeatedly-failing channel poll. Resets on the
    first success. State lives in the long-running supervisor process (cleared on
    restart). Stops a sustained fault — e.g. a Telegram 409 'terminated by other
    getUpdates request' (two pollers on one token) — from retrying (and logging)
    every tick."""

    def __init__(self, base: float, cap: float) -> None:
        self.base, self.cap = base, cap
        self.fails = 0
        self.skip_until = 0.0

    def should_skip(self, now: float) -> bool:
        return now < self.skip_until

    def record_success(self) -> None:
        self.fails = 0
        self.skip_until = 0.0

    def record_failure(self, now: float) -> float:
        self.fails += 1
        delay = min(self.base * (2 ** (self.fails - 1)), self.cap)
        self.skip_until = now + delay
        return delay


# 5s after the first failure, doubling to a 5-minute cap on a sustained fault.
_channel_backoff = _Backoff(5.0, 300.0)


def _poll_channels(paths: Any, long_poll_seconds: int = 0, *, clock: Any = time.monotonic) -> None:
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
    if _channel_backoff.should_skip(clock()):
        return  # in cooldown after repeated failures — don't hammer (or spam the log)
    try:
        ingest_once(paths.home, paths.logs, paths.tasks, paths.agents,
                    long_poll_seconds=long_poll_seconds, process_agents=True)
        _channel_backoff.record_success()
    except Exception as exc:  # noqa: BLE001 — the supervisor must survive any channel fault
        delay = _channel_backoff.record_failure(clock())
        append_event(paths.logs, "channel.ingest_error", status="error", error=str(exc),
                     consecutive=_channel_backoff.fails, retry_in_seconds=round(delay))


def _mail_wake_targets(home: Path, agents: dict[str, Any], *, skip: set[str]) -> list[str]:
    """Agents that need a mail wake: unread inbox messages, no pending tasks
    (`skip` = agents already in the tick's wake targets), and no mail-wake task
    already queued (a throttled wake must not stack a new task every tick)."""
    from jigga.core.config import load_teams
    from jigga.runtime.mailbox import unread_messages
    from jigga.runtime.tasks import list_tasks

    membership: dict[str, str] = {}
    for team in load_teams(Path(home) / "teams").values():
        for member in team.agents or []:
            member_id = member.get("id") if isinstance(member, dict) else None
            if member_id and member_id not in membership:
                membership[member_id] = team.id
    queued_mail_wakes = {
        task.assignee for task in list_tasks(Path(home) / "tasks")
        if task.state in ("pending", "claimed", "running") and (task.metadata or {}).get("mail_wake")
    }
    woken: list[str] = []
    for agent_id in agents:
        if agent_id in skip or agent_id in queued_mail_wakes:
            continue
        workspace = membership.get(agent_id, agent_id)
        if unread_messages(home, workspace, agent_id):
            woken.append(agent_id)
    return woken


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
    # Crash recovery: tasks stuck claimed/running and v2 nodes stuck running
    # past the staleness threshold are marked failed (visible, never silently
    # retried). Age-filtered → idempotent every tick; contained so a corrupt
    # record can't break the tick.
    try:
        from jigga.runtime.recovery import sweep_stale

        recovered = sweep_stale(paths)
        if recovered["tasks"] or recovered["nodes"]:
            append_event(paths.logs, "recovery.swept", **recovered)
    except Exception as exc:  # noqa: BLE001 — recovery must not break the tick it exists to protect
        append_event(paths.logs, "recovery.sweep_error", status="error", error=str(exc))
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
    # Proactive workflow discovery (once per interval, marker-guarded): surface
    # NEW high-confidence suggestions to the audit log + the user's channel + the
    # jiggaview Workflows badge. Contained so a discovery fault can't break the tick.
    try:
        from jigga.runtime.discovery import maybe_surface_suggestions

        surfaced = maybe_surface_suggestions(paths.home, paths.logs)
        if surfaced and surfaced.get("surfaced"):
            append_event(paths.logs, "workflow.discovery", surfaced=surfaced["surfaced"])
    except Exception as exc:  # noqa: BLE001 — discovery must not break the tick
        append_event(paths.logs, "workflow.discovery_error", status="error", error=str(exc))
    # Channels poll on the heartbeat (B2): enabled bots always respond whenever
    # the supervisor runs — no separate `jigga channels listen` needed. We only
    # create tasks here; the tick's own agent-waking loop below runs them, so
    # there's one execution path. Any channel/network error is contained so it
    # can't break the tick. `channel_long_poll_seconds` defaults to 0 (a single
    # non-blocking poll), but the supervisor loop passes a real long-poll timeout
    # when a channel is enabled, so a message is picked up the instant it arrives
    # rather than waiting for the next cron interval (near-real-time chat).
    _poll_channels(paths, long_poll_seconds=channel_long_poll_seconds)
    # Advance non-terminal v2 (DAG) workflow runs on the heartbeat: parked
    # approval nodes whose `approve <code>` arrived resume here, and a run
    # that outran its per-tick node budget continues. Bounded + contained so
    # a broken run can't break the tick.
    try:
        from jigga.runtime.workflow_engine import advance_all_runs

        advanced = advance_all_runs(paths)
        if advanced["advanced"]:
            append_event(paths.logs, "workflow.runs_advanced", runs=advanced["advanced"])
    except Exception as exc:  # noqa: BLE001 — run advancement must not break the tick
        append_event(paths.logs, "workflow.advance_error", status="error", error=str(exc))
    # One-shot reminders due by now become tasks for their target agent (fired
    # exactly once, bounded per sweep). Contained so a bad reminder file can't
    # break the tick.
    try:
        from jigga.runtime.reminders import fire_due_reminders

        fired = fire_due_reminders(paths.home, paths.logs, paths.tasks, paths.agents)
        if fired:
            append_event(paths.logs, "reminders.swept", fired=[r["id"] for r in fired])
    except Exception as exc:  # noqa: BLE001 — reminder sweep must not break the tick
        append_event(paths.logs, "reminder.sweep_error", status="error", error=str(exc))
    agents = load_agents(paths.agents)
    workflows = load_workflows(paths.workflows)
    events = due_events(paths.agents, paths.workflows, agents=agents, workflows=workflows)
    loop_state = load_loop_state(paths.home)
    now = now_utc()
    wake_limit = max_wakes_per_hour(paths.home)
    # Disabled agents/teams (config `disabled.*`): the supervisor never wakes
    # them — cron skipped, tasks stay pending (visible, never lost), mail
    # wakes withheld until re-enabled.
    from jigga.runtime.disabled import disabled_agent_ids
    disabled = disabled_agent_ids(paths.home, paths.teams)
    deduped_events: list[dict[str, Any]] = []
    skipped_events: list[dict[str, Any]] = []

    for event in events:
        event_dict = event.to_dict()
        if event.type == "cron.tick":
            cron = event.payload.get("cron", "")
            target = event.targets[0] if event.targets else ""
            if target in disabled:
                skipped_events.append({"reason": "agent.disabled", "event": event_dict})
                continue
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
    # Mail wake (W6 follow-up): an unread inbox message wakes its recipient
    # within a tick — like task assignment — instead of waiting for some other
    # reason to wake. Implemented as a created task so it rides the normal
    # pipeline: the wake-throttle below still applies (loop guard for two
    # agents pinging each other), the context pack surfaces the inbox, and a
    # successful run marks the mail read. Agents that already have pending
    # tasks need nothing — any run delivers their inbox.
    for agent_id in _mail_wake_targets(paths.home, agents, skip=set(targets) | disabled):
        create_task(
            paths.tasks,
            title="Unread mailbox messages",
            description=("You have unread messages — see the 'Your inbox' section of your "
                         "context. Act on them or note what matters in your MEMORY.md."),
            assignee=agent_id,
            metadata={"mail_wake": True},
        )
        append_event(paths.logs, "supervisor.mail_wake", agent=agent_id)
        targets.append(agent_id)
        pending_task_count += 1
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
        if agent_id in disabled:
            append_event(paths.logs, "supervisor.agent_disabled", status="ask", agent=agent_id)
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

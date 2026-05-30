from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_agents, load_workflows, max_wakes_per_hour
from jigga.core.models import now_iso
from jigga.core.paths import get_paths
from jigga.runtime.agent import run_agent
from jigga.runtime.audit import append_event, trace_context
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
from jigga.runtime.tasks import create_task, list_tasks
from jigga.runtime.workflow import run_workflow


def supervisor_tick(home: str | Path | None = None) -> dict[str, Any]:
    # One trace per tick: every event this tick produces — agent runs, workflow
    # runs, and the subagents they spawn — shares this id, so `jigga trace <id>`
    # returns the whole tick's causal tree.
    with trace_context():
        return _supervisor_tick(home)


def _supervisor_tick(home: str | Path | None = None) -> dict[str, Any]:
    paths = get_paths(home)
    agents = load_agents(paths.agents)
    workflows = load_workflows(paths.workflows)
    events = due_events(paths.agents, paths.workflows)
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
                run_workflow(paths.home, paths.logs, paths.workflows, paths.agents, paths.memory, workflow_id)
            deduped_events.append(event_dict)
        else:
            append_event(paths.logs, "event.created", **event_dict)
            deduped_events.append(event_dict)

    tasks = list_tasks(paths.tasks)
    targets = sorted({task.assignee for task in tasks if task.state == "pending" and task.assignee})
    append_event(
        paths.logs,
        "supervisor.tick",
        targets=targets,
        pending_task_count=len(tasks),
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

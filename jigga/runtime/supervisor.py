from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_agents, load_workflows
from jigga.core.models import now_iso
from jigga.core.paths import get_paths
from jigga.runtime.agent import run_agent
from jigga.runtime.audit import append_event
from jigga.runtime.state import read_state, write_state
from jigga.runtime.scheduler import due_events
from jigga.runtime.tasks import create_task, list_tasks
from jigga.runtime.workflow import run_workflow


def supervisor_tick(home: str | Path | None = None) -> dict[str, Any]:
    paths = get_paths(home)
    agents = load_agents(paths.agents)
    workflows = load_workflows(paths.workflows)
    events = due_events(paths.agents, paths.workflows)
    for event in events:
        append_event(paths.logs, "event.created", **event.to_dict())
        if event.type == "cron.tick":
            for agent_id in event.targets:
                create_task(
                    paths.tasks,
                    title=f"Scheduled wake: {event.payload.get('schedule', event.id)}",
                    assignee=agent_id,
                    metadata={"event": event.to_dict()},
                )
        elif event.type == "workflow.schedule_due":
            workflow_id = event.payload.get("workflow")
            if workflow_id in workflows:
                run_workflow(paths.home, paths.logs, paths.workflows, paths.agents, paths.memory, workflow_id)

    tasks = list_tasks(paths.tasks)
    targets = sorted({task.assignee for task in tasks if task.state == "pending" and task.assignee})
    append_event(paths.logs, "supervisor.tick", targets=targets, pending_task_count=len(tasks), event_count=len(events))

    runs = []
    for agent_id in targets:
        if agent_id not in agents:
            append_event(paths.logs, "supervisor.target_missing", status="failed", agent=agent_id)
            continue
        runs.append(run_agent(paths.home, paths.logs, paths.tasks, paths.agents, agent_id))

    state = read_state(paths.state)
    state.last_supervisor_tick_at = now_iso()
    write_state(paths.state, state)
    return {"events": [event.to_dict() for event in events], "targets": targets, "runs": runs}

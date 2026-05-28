from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_agents
from jigga.core.models import now_iso
from jigga.core.paths import get_paths
from jigga.runtime.agent import run_agent
from jigga.runtime.audit import append_event
from jigga.runtime.state import read_state, write_state
from jigga.runtime.tasks import list_tasks


def supervisor_tick(home: str | Path | None = None) -> dict[str, Any]:
    paths = get_paths(home)
    agents = load_agents(paths.agents)
    tasks = list_tasks(paths.tasks)
    targets = sorted({task.assignee for task in tasks if task.state == "pending" and task.assignee})
    append_event(paths.logs, "supervisor.tick", targets=targets, pending_task_count=len(tasks))

    runs = []
    for agent_id in targets:
        if agent_id not in agents:
            append_event(paths.logs, "supervisor.target_missing", status="failed", agent=agent_id)
            continue
        runs.append(run_agent(paths.home, paths.logs, paths.tasks, paths.agents, agent_id))

    state = read_state(paths.state)
    state.last_supervisor_tick_at = now_iso()
    write_state(paths.state, state)
    return {"targets": targets, "runs": runs}

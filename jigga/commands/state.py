from __future__ import annotations

from pathlib import Path

from jigga.core.config import load_agents, load_teams
from jigga.core.models import now_iso
from jigga.core.paths import get_paths
from jigga.runtime.state import read_state, write_state
from jigga.runtime.tasks import list_tasks


def inspect_state(home: str | Path | None = None) -> dict:
    paths = get_paths(home)
    agents = load_agents(paths.agents)
    teams = load_teams(paths.teams)
    tasks = list_tasks(paths.tasks)
    state = read_state(paths.state)
    loaded_at = now_iso()
    state.agents = {key: {"source": value.source, "loaded_at": loaded_at} for key, value in agents.items()}
    state.teams = {key: {"source": value.source, "loaded_at": loaded_at} for key, value in teams.items()}
    state.tasks = {task.id: {"state": task.state, "assignee": task.assignee, "updated_at": task.updated_at} for task in tasks}
    write_state(paths.state, state)
    return {
        "home": str(paths.home),
        "agents": sorted(agents),
        "teams": sorted(teams),
        "tasks": [task.to_dict() for task in tasks],
        "state": state.to_dict(),
    }

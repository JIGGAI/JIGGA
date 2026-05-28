from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_teams
from jigga.core.io import ensure_dir, write_json
from jigga.runtime.audit import append_event, new_id
from jigga.runtime.tasks import create_task
from jigga.runtime.workflow import run_workflow


def run_team(home: Path, logs_dir: Path, tasks_dir: Path, teams_dir: Path, workflows_dir: Path, agents_dir: Path, memory_dir: Path, team_id: str) -> dict[str, Any]:
    teams = load_teams(teams_dir)
    team = teams.get(team_id)
    if team is None:
        raise ValueError(f"Team not found: {team_id}")

    run_id = new_id("team_run")
    run_dir = home / "runs" / "teams" / team_id / run_id
    ensure_dir(run_dir)
    append_event(logs_dir, "team.run.started", team=team_id, run_id=run_id)

    created_tasks = []
    workflow_runs = []
    default_assignee = team.routing.get("default_assignee")
    if default_assignee:
        task = create_task(
            tasks_dir,
            title=f"Team {team_id} coordination task",
            description=f"Created by team runtime {run_id}.",
            assignee=default_assignee,
            metadata={"team_id": team_id, "run_id": run_id},
        )
        created_tasks.append(task.to_dict())
        append_event(logs_dir, "team.task_created", team=team_id, run_id=run_id, task_id=task.id, assignee=default_assignee)

    for workflow_id in team.default_workflows:
        result = run_workflow(home, logs_dir, workflows_dir, agents_dir, memory_dir, workflow_id)
        workflow_runs.append(result)

    record = {
        "id": run_id,
        "team_id": team_id,
        "created_tasks": created_tasks,
        "workflow_runs": workflow_runs,
        "run_dir": str(run_dir),
    }
    write_json(run_dir / "run.json", record)
    append_event(logs_dir, "team.run.completed", team=team_id, run_id=run_id)
    return record

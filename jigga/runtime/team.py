from __future__ import annotations

from typing import Any

from jigga.core.config import load_teams
from jigga.core.io import ensure_dir, write_json
from jigga.core.paths import JiggaPaths
from jigga.runtime.audit import append_event, new_id
from jigga.runtime.tasks import create_task
from jigga.runtime.workflow import run_workflow
from jigga.runtime.workspaces import scaffold_workspace


def run_team(
    paths: JiggaPaths,
    team_id: str,
) -> dict[str, Any]:
    home, logs_dir, tasks_dir, teams_dir = (
        paths.home, paths.logs, paths.tasks, paths.teams,
    )
    teams = load_teams(teams_dir)
    team = teams.get(team_id)
    if team is None:
        raise ValueError(f"Team not found: {team_id}")

    # Ensure the team's shared workspace exists (idempotent) — created on first
    # use regardless of how the team was created (yaml drop, examples, ...).
    scaffold_workspace(home, team)

    run_id = new_id("team_run")
    run_dir = home / "runs" / "teams" / team_id / run_id
    ensure_dir(run_dir)
    routing = team.routing or {}
    handoffs = routing.get("handoffs") or []
    append_event(
        logs_dir,
        "team.run.started",
        team=team_id,
        run_id=run_id,
        handoff_count=len(handoffs),
    )
    if handoffs:
        # Handoffs are declarative routing rules; they execute file-first when a
        # `from` member completes its team task (see jigga.runtime.handoffs —
        # fire_handoffs, wired into the agent completion path). Here we just
        # surface what's declared on the run for auditability.
        append_event(
            logs_dir,
            "team.handoffs_declared",
            team=team_id,
            run_id=run_id,
            handoffs=handoffs,
        )

    created_tasks = []
    workflow_runs = []
    default_assignee = routing.get("default_assignee")
    if default_assignee:
        from jigga.runtime.lanes import default_lane

        task = create_task(
            tasks_dir,
            title=f"Team {team_id} coordination task",
            description=f"Created by team runtime {run_id}.",
            assignee=default_assignee,
            lane=default_lane(team),  # land on the team's first lane (None if no board)
            metadata={
                "team_id": team_id,
                "run_id": run_id,
                "handoffs": handoffs,
            },
        )
        created_tasks.append(task.to_dict())
        append_event(
            logs_dir,
            "team.task_created",
            team=team_id,
            run_id=run_id,
            task_id=task.id,
            assignee=default_assignee,
        )

    for workflow_id in team.default_workflows:
        result = run_workflow(paths, workflow_id)
        workflow_runs.append(result)

    record = {
        "id": run_id,
        "team_id": team_id,
        "created_tasks": created_tasks,
        "workflow_runs": workflow_runs,
        "handoffs": handoffs,
        "run_dir": str(run_dir),
    }
    write_json(run_dir / "run.json", record)
    append_event(logs_dir, "team.run.completed", team=team_id, run_id=run_id)
    return record

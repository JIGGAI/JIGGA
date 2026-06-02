from __future__ import annotations

from pathlib import Path

from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.config import load_teams
from jigga.core.io import write_yaml
from jigga.core.models import WorkflowStep
from jigga.runtime.handlers import _team_insight_handler, _team_orchestration_handler
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.tasks import tasks_for_agent
from jigga.runtime.workspaces import scaffold_workspace, write_curated


def _ctx(paths, agent_id="chief"):
    return RuntimeContext(agent=type("A", (), {"id": agent_id})(), home=paths.home,
                          logs_dir=paths.logs, sessions_dir=paths.home / "sessions")


def _team(paths):
    write_yaml(paths.teams / "mt.yaml", {"id": "mt", "name": "Marketing", "purpose": "Launch",
               "agents": [{"id": "lead", "role": "lead"}, {"id": "writer", "role": "writer"}],
               "routing": {"default_assignee": "lead"}})
    team = load_teams(paths.teams)["mt"]
    scaffold_workspace(paths.home, team)
    write_curated(paths.home, team, "notes/plan.md", "PLAN: ship it", member="lead")
    return team


def test_team_list_returns_every_team(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths)
    out = _team_insight_handler(WorkflowStep(id="s", action="team.list"), None, {}, {}, _ctx(paths))
    assert out["teams"][0]["id"] == "mt"
    assert out["teams"][0]["lead"] == "lead" and "writer" in out["teams"][0]["members"]


def test_team_status_reads_plan_and_decisions(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths)
    out = _team_insight_handler(WorkflowStep(id="s", action="team.status"), None, {"team_id": "mt"}, {}, _ctx(paths))
    status = out["statuses"][0]
    assert status["team"] == "mt" and "ship it" in (status["plan"] or "")


def test_task_assign_creates_task_for_any_agent(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths)
    out = _team_orchestration_handler(
        WorkflowStep(id="s", action="task.assign"), None,
        {"assignee": "writer", "title": "draft the post", "team_id": "mt"}, {}, _ctx(paths))
    assert out["assignee"] == "writer"
    pend = tasks_for_agent(paths.tasks, "writer")
    assert len(pend) == 1 and pend[0].metadata["assigned_by"] == "chief"


def test_team_run_invokes_run_team(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths)
    with patch("jigga.runtime.team.run_team", return_value={"id": "run1", "team_id": "mt"}) as fake:
        out = _team_orchestration_handler(
            WorkflowStep(id="s", action="team.run"), None, {"team_id": "mt"}, {}, _ctx(paths))
    assert fake.called and out["team_id"] == "mt"


def test_orchestration_requires_args(tmp_path: Path) -> None:
    import pytest
    paths = init_runtime(tmp_path)
    with pytest.raises(ValueError):
        _team_orchestration_handler(WorkflowStep(id="s", action="task.assign"), None, {"title": "x"}, {}, _ctx(paths))

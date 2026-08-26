from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.agent import run_agent
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.tasks import create_task, list_tasks


def _result(content="done") -> ModelCallResult:
    return ModelCallResult(status="ok", provider="dry_run", model="m",
                           content=content, dry_run=True, tool_calls=[])


def _team(paths) -> None:
    write_yaml(paths.home / "teams" / "eng.yaml", {
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"}],
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
                  {"id": "ready-for-pr"}, {"id": "done"}],
    })
    for aid in ("eng-lead", "eng-dev"):
        write_yaml(paths.agents / f"{aid}.yaml", {
            "id": aid, "name": aid, "role": "r", "memory_scope": "task_only",
            "tools": [], "permissions": {}, "permission_mode": "autonomous"})


def test_a_finished_run_leaves_a_team_ticket_incomplete(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    create_task(paths.tasks, "ship it", assignee="eng-dev", lane="in-progress",
                metadata={"team_id": "eng"})

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    ticket = list_tasks(paths.tasks)[0]
    assert ticket.state != "completed"          # the run ended; the work did not
    assert ticket.assignee == "eng-lead"        # bounced to the lead
    assert ticket.lane == "backlog"
    assert ticket.metadata["bounces"] == 1


def test_a_ticket_in_done_completes(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    create_task(paths.tasks, "shipped", assignee="eng-dev", lane="done",
                metadata={"team_id": "eng"})

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    assert list_tasks(paths.tasks)[0].state == "completed"


def test_a_ticket_whose_team_cannot_be_resolved_does_not_complete(tmp_path: Path) -> None:
    # A lane means the lane must decide, and that requires the team. A yaml
    # deleted out from under a lane-managed ticket must not silently reopen
    # the plain-completion bug this task exists to fix.
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    create_task(paths.tasks, "orphaned", assignee="eng-dev", lane="in-progress",
                metadata={"team_id": "does-not-exist"})

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    ticket = list_tasks(paths.tasks)[0]
    assert ticket.state == "blocked"
    assert ticket.lane == "in-progress"          # left alone
    assert ticket.assignee == "eng-dev"          # left alone


def test_a_plain_task_is_untouched_by_any_of_this(tmp_path: Path) -> None:
    # No lane means no board; today's behaviour must survive exactly.
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    create_task(paths.tasks, "plain", assignee="eng-dev")

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    ticket = list_tasks(paths.tasks)[0]
    assert ticket.state == "completed"
    assert ticket.assignee == "eng-dev"

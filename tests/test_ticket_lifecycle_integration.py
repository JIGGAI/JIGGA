from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.agent import run_agent
from jigga.runtime.model_router import ModelCallResult
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.capabilities import bundled_capabilities
from jigga.runtime.handlers import _tickets_handler
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.tasks import create_task, find_task, list_tasks, set_task_state


def _result(content="done") -> ModelCallResult:
    return ModelCallResult(status="ok", provider="dry_run", model="m",
                           content=content, dry_run=True, tool_calls=[])


def _close(paths, actor: str, ticket_id: str):
    """Close a ticket the way the lead does — the sole route into `done`."""
    cap = next(c for c in bundled_capabilities() if "tickets.close" in c.actions)
    agent = AgentConfig(id=actor, name=actor, role="r", memory_scope="task_only",
                        tools=["tickets.close"], permissions={})
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    return _tickets_handler(WorkflowStep(id="s", action="tickets.close", input={}),
                            cap, {"ticket": ticket_id}, {}, runtime)


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
    # `done` still means `completed` — but the ticket has to have GOT there the
    # only way it can, by the lead closing it. Dropping a ticket straight into
    # `done` (or moving it there) is the ungated second door into completion
    # that `move_task_lane` now refuses, so setting the lane up that way would
    # be testing a route that no longer exists.
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    ticket = create_task(paths.tasks, "shipped", assignee="eng-dev", lane="ready-for-pr",
                         metadata={"team_id": "eng"})
    _close(paths, "eng-lead", ticket.id)

    fresh = find_task(paths.tasks, ticket.id)
    assert (fresh.lane, fresh.state) == ("done", "completed")

    # Re-queued (an operator re-running closed work): the run must leave it
    # complete rather than bouncing it back onto the board.
    set_task_state(paths.tasks, ticket.id, "pending")
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

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import json

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.agent import run_agent
from jigga.runtime.model_router import ModelCallResult
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.capabilities import bundled_capabilities
from jigga.runtime.handlers import _tickets_handler
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.tasks import create_task, find_task, list_tasks, set_task_state

PIPELINE_TRANSITIONS = {
    "rules": [
        {"from": "lead", "to": "dev", "lane": "in-progress"},
        {"from": "dev", "to": "test", "lane": "testing"},
        {"from": "test", "to": "dev", "lane": "in-progress"},
        {"from": "test", "to": "lead", "lane": "ready-for-pr"},
    ],
    "bounce_lane": "backlog",
}


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
        "lane_transitions": PIPELINE_TRANSITIONS,
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


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def test_a_bounced_ticket_is_not_announced_as_completed(tmp_path: Path) -> None:
    """`task.completed` / `agent.task_completed` were keyed on the RUN's state,
    so a run that had just bounced its ticket back to backlog still announced it
    as completed in the audit log — and `runtime/inference.py` mines
    `agent.task_completed`, so unfinished work fed the pattern miner as
    finished."""
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    create_task(paths.tasks, "ship it", assignee="eng-dev", lane="in-progress",
                metadata={"team_id": "eng"})

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    ticket = list_tasks(paths.tasks)[0]
    assert ticket.state == "pending" and ticket.lane == "backlog"   # it bounced
    types = [e["type"] for e in _events(paths)]
    assert "ticket.bounced" in types
    assert "task.completed" not in types
    assert "agent.task_completed" not in types


def test_a_blocked_ticket_is_not_announced_as_completed(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    create_task(paths.tasks, "looping", assignee="eng-dev", lane="in-progress",
                metadata={"team_id": "eng", "bounces": 3})

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    assert list_tasks(paths.tasks)[0].state == "blocked"
    types = [e["type"] for e in _events(paths)]
    assert "task.completed" not in types
    assert "agent.task_completed" not in types


def test_a_plain_task_is_still_announced_as_completed(tmp_path: Path) -> None:
    # No lane, no board: the run state IS the outcome, exactly as before.
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    create_task(paths.tasks, "plain", assignee="eng-dev")

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    types = [e["type"] for e in _events(paths)]
    assert "task.completed" in types
    assert "agent.task_completed" in types


def test_a_closed_ticket_is_announced_as_completed(tmp_path: Path) -> None:
    # The event still fires where it is true: the lane says the work is done.
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    ticket = create_task(paths.tasks, "shipped", assignee="eng-dev", lane="ready-for-pr",
                         metadata={"team_id": "eng"})
    _close(paths, "eng-lead", ticket.id)
    set_task_state(paths.tasks, ticket.id, "pending")

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    assert "task.completed" in [e["type"] for e in _events(paths)]


def test_a_ticket_that_has_bounced_too_often_blocks_through_a_real_run(tmp_path: Path) -> None:
    """`blocked` is the one irreversible outcome on the board — the supervisor
    stops waking anyone for it — and only the pure decider was covered. This
    walks it through an actual run_agent: the audit event, the persisted state,
    and the fact that the ticket is genuinely off the queue afterwards."""
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    ticket = create_task(paths.tasks, "ping-pong", assignee="eng-dev", lane="in-progress",
                         metadata={"team_id": "eng", "bounces": 3})

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    fresh = find_task(paths.tasks, ticket.id)
    assert fresh.state == "blocked"
    assert fresh.lane == "in-progress"       # the board is left exactly as it was
    assert fresh.assignee == "eng-dev"

    blocked = [e for e in _events(paths) if e["type"] == "ticket.blocked"]
    assert len(blocked) == 1
    assert blocked[0]["status"] == "ask"     # it wants a human, loudly
    assert blocked[0]["details"]["task_id"] == ticket.id
    assert blocked[0]["details"]["agent"] == "eng-dev"

    # It bounced nowhere and it did not silently re-bounce.
    assert not [e for e in _events(paths) if e["type"] == "ticket.bounced"]
    assert fresh.metadata["bounces"] == 3

    # And it stays off the queue: the next run has nothing to pick up.
    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        second = run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")
    assert second["processed_tasks"] == []
    assert find_task(paths.tasks, ticket.id).state == "blocked"
    assert len([e for e in _events(paths) if e["type"] == "ticket.blocked"]) == 1


def test_a_ticket_whose_team_vanished_blocks_through_a_real_run(tmp_path: Path) -> None:
    # The other route into `blocked`: the lane cannot decide without the team,
    # and guessing would reopen the completion bug this branch exists to fix.
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    ticket = create_task(paths.tasks, "orphaned", assignee="eng-dev", lane="in-progress",
                         metadata={"team_id": "does-not-exist"})

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    assert find_task(paths.tasks, ticket.id).state == "blocked"
    unresolved = [e for e in _events(paths) if e["type"] == "ticket.team_unresolved"]
    assert unresolved and unresolved[0]["details"]["task_id"] == ticket.id
    assert "task.completed" not in [e["type"] for e in _events(paths)]

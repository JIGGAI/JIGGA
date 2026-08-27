from __future__ import annotations

import json

from pathlib import Path

import pytest

from jigga.core.models import WorkflowStep
from jigga.runtime.capabilities import bundled_capabilities
from jigga.runtime.handlers import _tickets_handler
from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.decompose import decompose
from jigga.runtime.tasks import create_task, find_task, update_task

PIPELINE_TRANSITIONS = {
    "rules": [
        {"from": "lead", "to": "dev", "lane": "in-progress"},
        {"from": "dev", "to": "test", "lane": "testing"},
        {"from": "test", "to": "dev", "lane": "in-progress"},
        {"from": "test", "to": "lead", "lane": "ready-for-pr"},
    ],
    "bounce_lane": "backlog",
}


def _cap():
    return next(c for c in bundled_capabilities() if "tickets.close" in c.actions)

def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []



def _setup(tmp_path: Path):
    paths = init_runtime(tmp_path)
    write_yaml(paths.home / "teams" / "eng.yaml", {
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"},
                   {"id": "eng-test", "role": "test"}],
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
                  {"id": "ready-for-pr"}, {"id": "done"}],
        "lane_transitions": PIPELINE_TRANSITIONS,
    })
    return paths


def _runtime(paths, agent_id: str) -> RuntimeContext:
    agent = AgentConfig(id=agent_id, name=agent_id, role="r", memory_scope="task_only",
                        tools=["tickets.close"], permissions={})
    return RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                          sessions_dir=paths.home / "sessions")


def _close(paths, actor, payload):
    return _tickets_handler(WorkflowStep(id="s", action="tickets.close", input={}),
                            _cap(), payload, {}, _runtime(paths, actor))


def test_the_lead_closes_a_ready_ticket(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "ship", assignee="eng-lead", lane="ready-for-pr",
                    metadata={"team_id": "eng"})

    _close(paths, "eng-lead", {"ticket": t.id})

    fresh = find_task(paths.tasks, t.id)
    assert fresh.lane == "done"
    assert fresh.state == "completed"


def test_only_the_lead_may_close(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "ship", assignee="eng-dev", lane="ready-for-pr",
                    metadata={"team_id": "eng"})
    with pytest.raises(PermissionError):
        _close(paths, "eng-dev", {"ticket": t.id})
    assert find_task(paths.tasks, t.id).state != "completed"


def test_a_ticket_must_reach_ready_for_pr_first(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "ship", assignee="eng-lead", lane="in-progress",
                    metadata={"team_id": "eng"})
    with pytest.raises(ValueError):
        _close(paths, "eng-lead", {"ticket": t.id})
    assert find_task(paths.tasks, t.id).state != "completed"


def test_the_close_comment_reaches_the_audit_event(tmp_path: Path) -> None:
    """Declared on tickets.close, read nowhere. Closing is the one irreversible
    step on the board, so "how the work was confirmed done" is exactly the note
    that must not be dropped."""
    paths = _setup(tmp_path)
    ticket = create_task(paths.tasks, "shipped", assignee="eng-lead", lane="ready-for-pr",
                         metadata={"team_id": "eng"})

    result = _close(paths, "eng-lead", {"ticket": ticket.id, "comment": "PR #412 merged."})

    closed = [e for e in _events(paths) if e["type"] == "team.ticket.closed"]
    assert len(closed) == 1
    assert closed[0]["details"]["comment"] == "PR #412 merged."
    assert result["comment"] == "PR #412 merged."

STORIES = [{"title": "one", "description": "brief", "assignee": "eng-dev"},
           {"title": "two", "description": "brief", "assignee": "eng-dev"}]


def _decomposed(paths):
    """An epic waiting on two stories, exactly as the lead left it."""
    epic = create_task(paths.tasks, "New website", description="A website.",
                       assignee="eng-lead", lane="backlog", metadata={"team_id": "eng"})
    result = decompose(paths.tasks, paths.teams, ticket_id=epic.id, actor="eng-lead",
                       summary="Cut by surface.", plan="plans/x.md", stories=STORIES)
    return epic.id, result["stories"]


def test_closing_the_last_story_through_the_action_wakes_its_epic(tmp_path: Path) -> None:
    """tickets.close writes `completed` itself, outside any run's outcome hook —
    which only ever sees the run's own ticket. Closing BOTH children through the
    real action left the epic `waiting` forever; it self-healed only when the
    closed child happened to sit in some run's pending snapshot."""
    paths = _setup(tmp_path)
    epic_id, kids = _decomposed(paths)

    for kid in kids:
        update_task(paths.tasks, kid, lane="ready-for-pr")
        _close(paths, "eng-lead", {"ticket": kid})
        assert find_task(paths.tasks, kid).state == "completed"

    epic = find_task(paths.tasks, epic_id)
    assert epic.state == "pending", "the epic must wake when its last story is closed"
    assert epic.lane == "ready-for-pr"
    assert epic.assignee == "eng-lead"

    released = [e for e in _events(paths) if e["type"] == "ticket.epic.released"]
    assert len(released) == 1, "released exactly once, on the last close"
    assert released[0]["details"]["child"] == kids[-1]
    assert released[0]["details"]["reason"] == "children complete"


def test_a_release_that_blows_up_does_not_break_the_close(tmp_path: Path) -> None:
    """Waking the parent is a follow-on. The close has already written `done` to
    disk, so raising here would fail an action that cannot be redone."""
    from unittest.mock import patch

    paths = _setup(tmp_path)
    _epic_id, kids = _decomposed(paths)
    update_task(paths.tasks, kids[0], lane="ready-for-pr")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("tasks dir went away")

    with patch("jigga.runtime.decompose.release_parent_if_ready", _boom):
        result = _close(paths, "eng-lead", {"ticket": kids[0]})

    assert result["state"] == "completed"
    assert find_task(paths.tasks, kids[0]).state == "completed"
    failed = [e for e in _events(paths) if e["type"] == "ticket.epic.release_failed"]
    assert len(failed) == 1
    assert "tasks dir went away" in failed[0]["details"]["reason"]

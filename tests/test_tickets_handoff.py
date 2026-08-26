from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.agent import _parameters_for
from jigga.runtime.capabilities import bundled_capabilities
from jigga.runtime.handlers import _tickets_handler
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.tasks import create_task, find_task

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
    return next(c for c in bundled_capabilities() if "tickets.handoff" in c.actions)

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
                        tools=["tickets.handoff"], permissions={})
    return RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                          sessions_dir=paths.home / "sessions")


def _handoff(paths, actor, payload):
    return _tickets_handler(WorkflowStep(id="s", action="tickets.handoff", input={}),
                            _cap(), payload, {}, _runtime(paths, actor))


def test_the_schema_names_the_real_fields() -> None:
    schema = _parameters_for("tickets.handoff", _cap())
    assert set(schema["properties"]) >= {"ticket", "assignee", "comment"}
    assert set(schema.get("required", [])) >= {"ticket", "assignee"}


def test_handoff_moves_assignee_lane_and_state_together(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "build", assignee="eng-dev", lane="in-progress",
                    metadata={"team_id": "eng"})

    _handoff(paths, "eng-dev", {"ticket": t.id, "assignee": "eng-test"})

    fresh = find_task(paths.tasks, t.id)
    assert fresh.assignee == "eng-test"
    assert fresh.lane == "testing"
    assert fresh.state == "pending"


def test_no_second_ticket_is_created(tmp_path: Path) -> None:
    # The whole point: one ticket travels the board.
    from jigga.runtime.tasks import list_tasks
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "build", assignee="eng-dev", lane="in-progress",
                    metadata={"team_id": "eng"})
    _handoff(paths, "eng-dev", {"ticket": t.id, "assignee": "eng-test"})
    assert [x.id for x in list_tasks(paths.tasks)] == [t.id]


def test_an_underived_transition_keeps_the_lane_and_says_so(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "build", assignee="eng-dev", lane="in-progress",
                    metadata={"team_id": "eng"})

    result = _handoff(paths, "eng-dev", {"ticket": t.id, "assignee": "eng-lead"})

    assert find_task(paths.tasks, t.id).lane == "in-progress"
    assert result["lane_derived"] is False
    events = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines() if line.strip()]
    assert "ticket.lane.underived" in [e["type"] for e in events]


def test_handoff_requires_a_ticket_and_an_assignee(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    with pytest.raises(ValueError):
        _handoff(paths, "eng-dev", {"ticket": "task_x"})


def test_a_handoff_resets_the_bounce_budget(tmp_path: Path) -> None:
    """`bounces` was written once and never cleared, making it a LIFETIME
    budget: a ticket that bounced three times across its whole history was
    permanently one non-handoff run from `blocked`, recoverable only by
    hand-editing JSON. Finding an owner is not looping."""
    from jigga.core.models import TeamConfig
    from jigga.runtime.ticket_outcome import resolve_ticket_outcome

    paths = _setup(tmp_path)
    ticket = create_task(paths.tasks, "much-travelled", assignee="eng-lead",
                         lane="backlog", metadata={"team_id": "eng", "bounces": 3,
                                                   "keep": "me"})
    team = TeamConfig.from_dict({
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"},
                   {"id": "eng-test", "role": "test"}],
        "lane_transitions": PIPELINE_TRANSITIONS,
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
                  {"id": "ready-for-pr"}, {"id": "done"}]})

    # At the ceiling, the next unowned run blocks it for good.
    assert resolve_ticket_outcome(find_task(paths.tasks, ticket.id), team,
                                  run_state="completed")["state"] == "blocked"

    _handoff(paths, "eng-lead", {"ticket": ticket.id, "assignee": "eng-dev"})

    fresh = find_task(paths.tasks, ticket.id)
    assert fresh.metadata["bounces"] == 0
    assert fresh.metadata["keep"] == "me"          # nothing else in metadata is lost
    assert fresh.metadata["team_id"] == "eng"
    # ...and it is no longer one run away from blocked.
    assert resolve_ticket_outcome(fresh, team, run_state="completed")["state"] == "pending"


def test_the_handoff_comment_reaches_the_audit_event(tmp_path: Path) -> None:
    """`comment` is declared on tickets.handoff in capabilities.py and every
    engineering role is instructed to write one — and the handler read it
    nowhere. This codebase has already shipped a production bug of exactly that
    shape (a declared field silently discarded)."""
    paths = _setup(tmp_path)
    ticket = create_task(paths.tasks, "ship it", assignee="eng-dev", lane="in-progress",
                         metadata={"team_id": "eng"})

    result = _handoff(paths, "eng-dev", {"ticket": ticket.id, "assignee": "eng-test",
                                         "comment": "Rewrote the parser; run pytest -q."})

    handoffs = [e for e in _events(paths) if e["type"] == "team.ticket.handoff"]
    assert len(handoffs) == 1
    assert handoffs[0]["details"]["comment"] == "Rewrote the parser; run pytest -q."
    assert result["comment"] == "Rewrote the parser; run pytest -q."


def test_a_handoff_without_a_comment_records_none(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    ticket = create_task(paths.tasks, "ship it", assignee="eng-dev", lane="in-progress",
                         metadata={"team_id": "eng"})
    _handoff(paths, "eng-dev", {"ticket": ticket.id, "assignee": "eng-test"})
    handoffs = [e for e in _events(paths) if e["type"] == "team.ticket.handoff"]
    assert handoffs[0]["details"]["comment"] is None

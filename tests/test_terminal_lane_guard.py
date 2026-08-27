"""`done` has one door, and `tickets.move` is not it.

`move_task_lane` enforces a lane's gate on LEAVING a lane, never on entering
one. Once reaching `done` is what marks a ticket `completed`, any agent holding
`tickets.move` could put its own ticket there and have the runtime complete it —
no lead check, no close-lane check, no audit. A dev did exactly that.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.config import load_teams
from jigga.core.io import write_yaml
from jigga.runtime.lanes import (
    LaneGateError,
    is_lifecycle_managed,
    move_task_lane,
)
from jigga.runtime.tasks import create_task, find_task
from jigga.runtime.workspaces import scaffold_workspace

PIPELINE_TRANSITIONS = {
    "rules": [
        {"from": "lead", "to": "dev", "lane": "in-progress"},
        {"from": "dev", "to": "test", "lane": "testing"},
        {"from": "test", "to": "dev", "lane": "in-progress"},
        {"from": "test", "to": "lead", "lane": "ready-for-pr"},
    ],
    "bounce_lane": "backlog",
}

PIPELINE = [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
            {"id": "ready-for-pr"}, {"id": "done"}]


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _team(paths, *, lanes, agents=None, transitions=None):
    write_yaml(paths.teams / "eng.yaml", {
        "id": "eng", "name": "Eng",
        "agents": agents if agents is not None else [
            {"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"},
            {"id": "eng-test", "role": "test"}],
        "lanes": lanes,
        # Core declares no board shape; a fixture wanting the pipeline says so.
        **({"lane_transitions": transitions or PIPELINE_TRANSITIONS}
           if isinstance(lanes, list) and any(x.get("id") == "ready-for-pr" for x in lanes) else {}),
    })
    team = load_teams(paths.teams)["eng"]
    scaffold_workspace(paths.home, team)
    return team


def _move(paths, task_id, to_lane, actor):
    return move_task_lane(paths.home, paths.tasks, paths.logs, paths.teams,
                          task_id, to_lane, actor=actor)


def test_a_dev_cannot_move_its_own_ticket_into_done(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, lanes=PIPELINE)
    ticket = create_task(paths.tasks, "ship it", assignee="eng-dev", lane="in-progress",
                         metadata={"team_id": "eng"})

    with pytest.raises(LaneGateError) as exc:
        _move(paths, ticket.id, "done", "eng-dev")
    assert "tickets.close" in str(exc.value)

    fresh = find_task(paths.tasks, ticket.id)
    assert fresh.lane == "in-progress"          # the board did not move
    assert fresh.state != "completed"


def test_even_the_lead_closes_rather_than_moves(tmp_path: Path) -> None:
    # The refusal is about the door, not the actor: `tickets.close` is where the
    # lead check and the close-lane check live, so nobody bypasses it.
    paths = init_runtime(tmp_path)
    _team(paths, lanes=PIPELINE)
    ticket = create_task(paths.tasks, "merged", assignee="eng-lead", lane="ready-for-pr",
                         metadata={"team_id": "eng"})

    with pytest.raises(LaneGateError):
        _move(paths, ticket.id, "done", "eng-lead")
    assert find_task(paths.tasks, ticket.id).lane == "ready-for-pr"


def test_the_refusal_is_audited(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, lanes=PIPELINE)
    ticket = create_task(paths.tasks, "ship it", assignee="eng-dev", lane="in-progress",
                         metadata={"team_id": "eng"})

    with pytest.raises(LaneGateError):
        _move(paths, ticket.id, "done", "eng-dev")

    refusals = [e for e in _events(paths) if e["type"] == "team.ticket.move.refused"]
    assert len(refusals) == 1
    assert refusals[0]["details"]["to_lane"] == "done"
    assert refusals[0]["details"]["actor"] == "eng-dev"
    assert refusals[0].get("status") == "deny"


def test_every_other_lane_still_moves(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, lanes=PIPELINE)
    ticket = create_task(paths.tasks, "ship it", assignee="eng-dev", lane="in-progress",
                         metadata={"team_id": "eng"})
    assert _move(paths, ticket.id, "testing", "eng-dev").lane == "testing"


def test_a_board_that_is_not_running_the_lifecycle_keeps_its_done_lane(tmp_path: Path) -> None:
    # The `lanes: true` shorthand gives backlog/working/review/done — no
    # transition rule can target any of those, so this board never runs the
    # lifecycle and `done` never means `completed` on it. Refusing the move
    # would take away its terminal column for nothing.
    paths = init_runtime(tmp_path)
    team = _team(paths, lanes=True)
    assert not is_lifecycle_managed(team)
    ticket = create_task(paths.tasks, "note", assignee="eng-dev", lane="working",
                         metadata={"team_id": "eng"})
    assert _move(paths, ticket.id, "done", "eng-dev").lane == "done"

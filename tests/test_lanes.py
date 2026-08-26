from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.config import load_teams
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig, TeamConfig
from jigga.runtime.handoffs import fire_handoffs
from jigga.runtime.lanes import (
    DEFAULT_LANES,
    LaneError,
    LaneGateError,
    default_lane,
    move_task_lane,
    render_lanes,
    team_lanes,
)
from jigga.runtime.tasks import create_task, find_task
from jigga.runtime.workspaces import scaffold_workspace
from jigga.cli import main


def _team(paths, *, lanes, members=("strategist", "writer", "editor")) -> TeamConfig:
    write_yaml(paths.teams / "ct.yaml", {
        "id": "ct", "name": "Content Team",
        "agents": [{"id": m, "role": m} for m in members],
        "routing": {"default_assignee": members[0], "handoffs": []},
        "lanes": lanes,
    })
    for m in members:
        write_yaml(paths.agents / f"{m}.yaml", {"id": m, "name": m, "role": "x",
                   "memory_scope": "task_only", "tools": [], "permissions": {}})
    team = load_teams(paths.teams)["ct"]
    scaffold_workspace(paths.home, team)
    return team


# --- vocabulary normalization ----------------------------------------------


def test_team_lanes_shorthand_true_uses_defaults() -> None:
    team = TeamConfig(id="t", name="T", lanes=True)
    assert [lane.id for lane in team_lanes(team)] == [d["id"] for d in DEFAULT_LANES]
    assert default_lane(team) == "backlog"


def test_team_lanes_absent_or_false_is_no_board() -> None:
    assert team_lanes(TeamConfig(id="t", name="T")) == []
    assert team_lanes(TeamConfig(id="t", name="T", lanes=False)) == []
    assert default_lane(TeamConfig(id="t", name="T")) is None


def test_team_lanes_full_list_with_gate() -> None:
    team = TeamConfig(id="t", name="T",
                      agents=[{"id": "a", "role": "review"}],
                      lanes=[{"id": "brief", "description": "in"},
                             {"id": "review", "gate": "review"},
                             "published"])  # bare-string lane is allowed
    lanes = team_lanes(team)
    assert [lane.id for lane in lanes] == ["brief", "review", "published"]
    assert lanes[1].gate == "review"
    assert lanes[0].description == "in"


def test_team_lanes_rejects_duplicate_ids() -> None:
    team = TeamConfig(id="t", name="T", lanes=[{"id": "a"}, {"id": "a"}])
    with pytest.raises(LaneError, match="duplicate"):
        team_lanes(team)


def test_team_lanes_rejects_gate_naming_non_member() -> None:
    team = TeamConfig(id="t", name="T", agents=[{"id": "a", "role": "dev"}],
                      lanes=[{"id": "x", "gate": "ghost"}])
    with pytest.raises(LaneError, match="not a team member"):
        team_lanes(team)


# --- default lane on team-task creation ------------------------------------


def test_handoff_does_not_create_a_task_on_a_lane_managed_team(tmp_path: Path) -> None:
    """fire_handoffs used to spawn a second ticket that landed on the team's
    first lane; a lane-managed team now hands work on by reassigning the
    ticket it already has (tickets.handoff, Task 4), so a second spawned
    ticket would just fragment the board. See test_handoffs.py for the
    fire_handoffs-level coverage of this no-op (including the skip audit
    event) — this test guards that the "lands on first lane" behaviour this
    file used to assert is gone, not merely renamed."""
    paths = init_runtime(tmp_path)
    write_yaml(paths.teams / "ct.yaml", {
        "id": "ct", "name": "Content Team",
        "agents": [{"id": "strategist", "role": "strategist"}, {"id": "writer", "role": "writer"}],
        "routing": {"default_assignee": "strategist",
                    "handoffs": [{"from": "strategist", "to": "writer", "when": "ready"}]},
        "lanes": [{"id": "brief"}, {"id": "drafting"}],
    })
    created = fire_handoffs(paths.home, paths.logs, paths.tasks, paths.teams,
                            team_id="ct", from_member="strategist")
    assert created == []


# --- move + gate enforcement -----------------------------------------------


def _ticket(paths, lane="brief") -> str:
    task = create_task(paths.tasks, "ticket", assignee="writer",
                       metadata={"team_id": "ct"}, lane=lane)
    return task.id


def test_move_task_lane_happy_path(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, lanes=[{"id": "brief"}, {"id": "drafting"}])
    tid = _ticket(paths)
    moved = move_task_lane(paths.home, paths.tasks, paths.logs, paths.teams, tid, "drafting")
    assert moved.lane == "drafting"
    assert find_task(paths.tasks, tid).lane == "drafting"


def test_move_rejects_unknown_lane(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, lanes=[{"id": "brief"}, {"id": "drafting"}])
    tid = _ticket(paths)
    with pytest.raises(LaneError, match="Unknown lane"):
        move_task_lane(paths.home, paths.tasks, paths.logs, paths.teams, tid, "nope")


def test_move_gate_blocks_non_gate_actor_and_allows_gate(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    # 'review' lane gated by the 'editor' role; only editor moves a ticket OUT.
    _team(paths, lanes=[{"id": "brief"}, {"id": "review", "gate": "editor"}, {"id": "done"}])
    tid = _ticket(paths, lane="review")

    with pytest.raises(LaneGateError, match="gated by"):
        move_task_lane(paths.home, paths.tasks, paths.logs, paths.teams, tid, "done", actor="writer")
    # the gate member (by role) is allowed
    moved = move_task_lane(paths.home, paths.tasks, paths.logs, paths.teams, tid, "done", actor="editor")
    assert moved.lane == "done"


def test_move_non_team_task_is_rejected(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, lanes=True)
    solo = create_task(paths.tasks, "solo")  # no team_id
    with pytest.raises(LaneError, match="not a team task"):
        move_task_lane(paths.home, paths.tasks, paths.logs, paths.teams, solo.id, "working")


# --- CLI -------------------------------------------------------------------


def test_cli_team_lanes_json(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, lanes=[{"id": "brief", "description": "in"}, {"id": "review", "gate": "editor"}])
    assert main(["--home", str(tmp_path), "team", "lanes", "ct", "--json"]) == 0
    lanes = json.loads(capsys.readouterr().out)
    assert [lane["id"] for lane in lanes] == ["brief", "review"]
    assert lanes[1]["gate"] == "editor"


def test_cli_task_move_and_list_lane(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, lanes=[{"id": "brief"}, {"id": "drafting"}])
    tid = _ticket(paths)
    assert main(["--home", str(tmp_path), "task", "move", tid, "drafting"]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "task", "list", "--lane", "drafting", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [t["id"] for t in listed] == [tid] and listed[0]["lane"] == "drafting"


def test_cli_task_move_gate_blocks(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, lanes=[{"id": "review", "gate": "editor"}, {"id": "done"}])
    tid = _ticket(paths, lane="review")
    assert main(["--home", str(tmp_path), "task", "move", tid, "done", "--as", "writer"]) == 1
    assert "gated" in capsys.readouterr().out.lower()
    assert main(["--home", str(tmp_path), "task", "move", tid, "done", "--as", "editor"]) == 0


# --- context injection -----------------------------------------------------


def test_render_lanes_block() -> None:
    team = TeamConfig(id="t", name="T", agents=[{"id": "a", "role": "review"}],
                      lanes=[{"id": "brief", "description": "Incoming"},
                             {"id": "review", "gate": "review"}])
    text = render_lanes(team)
    assert "brief — Incoming" in text
    assert "review" in text and "[gate: review]" in text


# --- tickets capability handler --------------------------------------------


def test_tickets_capability_moves_and_enforces_gate(tmp_path: Path) -> None:
    from jigga.runtime.handlers import _tickets_handler
    from jigga.runtime.runtime_context import RuntimeContext

    paths = init_runtime(tmp_path)
    _team(paths, lanes=[{"id": "review", "gate": "editor"}, {"id": "done"}])
    tid = _ticket(paths, lane="review")

    def run(actor: str, action_args: dict):
        agent = AgentConfig(id=actor, name=actor, role=actor)
        ctx = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
        return _tickets_handler(None, None, action_args, {}, ctx)

    # writer (non-gate) is blocked by the lane gate
    with pytest.raises(LaneGateError):
        run("writer", {"action": "move", "task": tid, "lane": "done"})
    # editor (the gate) moves it; the agent IS the actor
    result = run("editor", {"action": "move", "task": tid, "lane": "done"})
    assert result["moved"]["lane"] == "done" and result["actor"] == "editor"
    # list returns the team's tickets with their lanes
    listing = run("editor", {"action": "list", "team": "ct"})
    assert listing["lanes"] == ["review", "done"]
    assert any(t["id"] == tid and t["lane"] == "done" for t in listing["tickets"])

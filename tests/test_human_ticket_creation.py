"""A person can file a ticket onto a team's board.

Lanes, gates and the whole ticket board existed, but only agents could put work
on them: `team run`, a handoff, or an agent's `task.assign` set `team_id`, and a
task created from the CLI had neither team nor lane. So a human's work sat
outside the board its team lives on — you could look at the board and move
tickets, but not file one.

Found while porting the ClawRecipes development team, whose entire premise is a
board a person triages.
"""

from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime


def _team(tmp_path: Path, capsys) -> None:
    assert main(["--home", str(tmp_path), "recipes", "scaffold", "development-team",
                 "--id", "eng"]) == 0
    capsys.readouterr()


def _created(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_a_ticket_lands_on_the_teams_first_lane(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    _team(tmp_path, capsys)
    assert main(["--home", str(tmp_path), "task", "create", "--title", "Fix the flaky test",
                 "--team", "eng", "--assignee", "eng-dev"]) == 0
    task = _created(capsys)
    assert task["lane"] == "backlog"
    assert task["metadata"]["team_id"] == "eng"


def test_a_starting_lane_can_be_chosen(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    _team(tmp_path, capsys)
    assert main(["--home", str(tmp_path), "task", "create", "--title", "Hotfix",
                 "--team", "eng", "--lane", "in-progress"]) == 0
    assert _created(capsys)["lane"] == "in-progress"


def test_an_unknown_lane_is_refused_with_the_real_ones(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    _team(tmp_path, capsys)
    assert main(["--home", str(tmp_path), "task", "create", "--title", "x",
                 "--team", "eng", "--lane", "nope"]) == 1
    out = capsys.readouterr().out
    assert "No lane 'nope'" in out and "backlog" in out


def test_an_unknown_team_is_refused(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "task", "create", "--title", "x", "--team", "ghost"]) == 1
    assert "No such team" in capsys.readouterr().out


def test_a_team_without_a_board_says_so(tmp_path: Path, capsys) -> None:
    # A team with no `lanes:` is not broken — it just has no board to file onto,
    # and saying that is more useful than inventing a lane.
    from jigga.core.io import write_yaml

    init_runtime(tmp_path)
    write_yaml(tmp_path / "teams" / "plain.yaml",
               {"id": "plain", "name": "Plain", "agents": [{"id": "plain-lead", "role": "lead"}]})
    assert main(["--home", str(tmp_path), "task", "create", "--title", "x", "--team", "plain"]) == 1
    assert "no ticket board" in capsys.readouterr().out


def test_a_lane_without_a_team_is_refused(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "task", "create", "--title", "x",
                 "--lane", "backlog"]) == 1
    assert "--lane needs --team" in capsys.readouterr().out


def test_a_plain_task_is_unchanged(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "task", "create", "--title", "Just a task"]) == 0
    task = _created(capsys)
    assert task["lane"] is None and not (task.get("metadata") or {}).get("team_id")


def test_the_filed_ticket_obeys_the_gate(tmp_path: Path, capsys) -> None:
    """The point of filing onto a board: the board's rules then apply to it."""
    init_runtime(tmp_path)
    _team(tmp_path, capsys)
    assert main(["--home", str(tmp_path), "task", "create", "--title", "Gated",
                 "--team", "eng", "--assignee", "eng-dev"]) == 0
    task_id = _created(capsys)["id"]

    assert main(["--home", str(tmp_path), "task", "move", task_id, "testing", "--as", "eng-dev"]) == 0
    capsys.readouterr()
    # `testing` is gated by QA — the author cannot wave their own work through.
    assert main(["--home", str(tmp_path), "task", "move", task_id, "ready-for-pr",
                 "--as", "eng-dev"]) == 1
    assert "gated by 'test'" in capsys.readouterr().out
    assert main(["--home", str(tmp_path), "task", "move", task_id, "ready-for-pr",
                 "--as", "eng-test"]) == 0

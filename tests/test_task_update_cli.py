"""`jigga task update` at the CLI boundary, plus the audit trail for tickets a
person files or edits by hand (the dashboard's ticket board goes through here)."""

from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.runtime.tasks import find_task


def _events(home: Path) -> list[dict]:
    log = home / "logs" / "events.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _create(tmp_path: Path, capsys, *extra: str) -> dict:
    assert main(["--home", str(tmp_path), "task", "create", "--title", "Original", *extra]) == 0
    return json.loads(capsys.readouterr().out)


def test_update_edits_the_task(tmp_path: Path, capsys) -> None:
    task = _create(tmp_path, capsys)
    assert main(["--home", str(tmp_path), "task", "update", task["id"],
                 "--title", "Edited", "--assignee", "dev"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["title"] == "Edited"
    assert out["assignee"] == "dev"
    assert find_task(tmp_path / "tasks", task["id"]).title == "Edited"


def test_update_with_no_fields_is_an_error(tmp_path: Path, capsys) -> None:
    task = _create(tmp_path, capsys)
    assert main(["--home", str(tmp_path), "task", "update", task["id"]]) == 1
    assert "at least one of" in capsys.readouterr().out


def test_update_unknown_task_exits_nonzero(tmp_path: Path, capsys) -> None:
    assert main(["--home", str(tmp_path), "task", "update", "task_nope", "--title", "x"]) == 1
    assert "Task not found" in capsys.readouterr().out


def test_create_and_update_are_audited_with_the_actor(tmp_path: Path, capsys) -> None:
    task = _create(tmp_path, capsys, "--as", "rj")
    assert main(["--home", str(tmp_path), "task", "update", task["id"],
                 "--title", "Edited", "--as", "rj"]) == 0
    capsys.readouterr()
    by_type = {e["type"]: e for e in _events(tmp_path)}
    assert "task.created" in by_type, "filing a ticket by hand should be attributable"
    assert "task.updated" in by_type, "editing a ticket by hand should be attributable"
    # top-level `actor`, not buried in details — that is what `jigga audit
    # --actor` filters on.
    assert by_type["task.created"]["actor"] == "rj"
    assert by_type["task.updated"]["actor"] == "rj"
    assert by_type["task.updated"]["details"]["fields"] == ["title"]


def test_update_leaves_the_lane_alone(tmp_path: Path, capsys) -> None:
    # A generic setter must not become a way around a gated lane.
    assert main(["--home", str(tmp_path), "recipes", "scaffold", "seven-development-team"]) == 0
    capsys.readouterr()
    task = _create(tmp_path, capsys, "--team", "seven_development_team", "--lane", "testing")
    assert main(["--home", str(tmp_path), "task", "update", task["id"], "--title", "Edited"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["lane"] == "testing"

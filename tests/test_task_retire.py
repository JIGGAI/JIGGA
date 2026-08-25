"""Taking a ticket off the board — archive (recoverable) and delete (not).

Two things matter. The two verbs must actually differ: archive keeps the file,
delete does not, and a test that only checks "it left the board" would pass for
both while one of them silently destroyed work. And the lane gate applies to
both, because otherwise "QA has not passed this" is one click from "then get
rid of it" and the gate binds only the people who play fair.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.runtime.lanes import LaneError, LaneGateError, archive_ticket, delete_ticket
from jigga.runtime.tasks import archive_task, create_task, destroy_task, find_task, list_tasks

TEAM = {
    "id": "t", "name": "T",
    "agents": [{"id": "t-dev", "role": "dev"}, {"id": "t-qa", "role": "test"}],
    "lanes": [{"id": "backlog"}, {"id": "testing", "gate": "test"}],
}


def _home(tmp_path: Path) -> Path:
    (tmp_path / "teams").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "teams" / "t.yaml").write_text(json.dumps(TEAM))  # yaml loads json
    return tmp_path


def _ticket(home: Path, lane: str):
    return create_task(home / "tasks", "A ticket", lane=lane, metadata={"team_id": "t"})


def _archived(home: Path, task_id: str) -> Path:
    return home / "tasks" / "archive" / f"{task_id}.json"


def _events(home: Path) -> list[dict]:
    log = home / "logs" / "events.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# --- the store half: the two verbs must actually differ ---------------------


def test_archive_keeps_the_file(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    task = create_task(tasks, "Recoverable")
    archive_task(tasks, task.id)
    assert not (tasks / f"{task.id}.json").exists()
    assert (tasks / "archive" / f"{task.id}.json").exists()


def test_delete_keeps_nothing(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    task = create_task(tasks, "Gone for good")
    destroy_task(tasks, task.id)
    assert not (tasks / f"{task.id}.json").exists()
    assert not (tasks / "archive" / f"{task.id}.json").exists()


def test_both_leave_the_live_set(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    a, b = create_task(tasks, "A"), create_task(tasks, "B")
    archive_task(tasks, a.id)
    destroy_task(tasks, b.id)
    assert find_task(tasks, a.id) is None
    assert find_task(tasks, b.id) is None
    assert list_tasks(tasks) == []


def test_delete_can_empty_the_archive(tmp_path: Path) -> None:
    # otherwise archiving is a one-way door and archive/ only clears by hand
    tasks = tmp_path / "tasks"
    task = create_task(tasks, "Archived first")
    archive_task(tasks, task.id)
    destroy_task(tasks, task.id)
    assert not (tasks / "archive" / f"{task.id}.json").exists()


@pytest.mark.parametrize("fn", [archive_task, destroy_task])
def test_unknown_task_raises(tmp_path: Path, fn) -> None:
    with pytest.raises(ValueError, match="Task not found"):
        fn(tmp_path / "tasks", "task_nope")


# --- the gate half ----------------------------------------------------------


@pytest.mark.parametrize("retire", [archive_ticket, delete_ticket])
def test_ungated_lane_needs_no_actor(tmp_path: Path, retire) -> None:
    home = _home(tmp_path)
    task = _ticket(home, "backlog")
    retire(home, home / "tasks", home / "logs", home / "teams", task.id)
    assert find_task(home / "tasks", task.id) is None


@pytest.mark.parametrize("retire", [archive_ticket, delete_ticket])
def test_gated_lane_refuses_the_wrong_actor(tmp_path: Path, retire) -> None:
    home = _home(tmp_path)
    task = _ticket(home, "testing")
    with pytest.raises(LaneGateError, match="gated by 'test'"):
        retire(home, home / "tasks", home / "logs", home / "teams", task.id, actor="t-dev")
    # a refused retirement must not half-happen
    assert find_task(home / "tasks", task.id) is not None
    assert not _archived(home, task.id).exists()


@pytest.mark.parametrize("actor", ["t-qa", "test"])   # member id, or its role
def test_the_gate_holder_may_retire(tmp_path: Path, actor: str) -> None:
    home = _home(tmp_path)
    task = _ticket(home, "testing")
    archive_ticket(home, home / "tasks", home / "logs", home / "teams", task.id, actor=actor)
    assert find_task(home / "tasks", task.id) is None


def test_a_task_with_no_team_has_no_gate(tmp_path: Path) -> None:
    home = _home(tmp_path)
    task = create_task(home / "tasks", "Plain task")
    delete_ticket(home, home / "tasks", home / "logs", home / "teams", task.id)
    assert find_task(home / "tasks", task.id) is None


def test_archive_and_delete_are_audited_apart(tmp_path: Path) -> None:
    home = _home(tmp_path)
    a, b = _ticket(home, "backlog"), _ticket(home, "backlog")
    archive_ticket(home, home / "tasks", home / "logs", home / "teams", a.id, actor="t-dev")
    delete_ticket(home, home / "tasks", home / "logs", home / "teams", b.id, actor="t-dev")
    types = [e["type"] for e in _events(home)]
    assert "team.ticket.archived" in types
    assert "team.ticket.deleted" in types


def test_the_act_as_identity_lands_where_audit_filters_on_it(tmp_path: Path) -> None:
    # top-level `actor`, not buried in details — that is what `jigga audit
    # --actor` reads.
    home = _home(tmp_path)
    task = _ticket(home, "backlog")
    delete_ticket(home, home / "tasks", home / "logs", home / "teams", task.id, actor="t-dev")
    deleted = [e for e in _events(home) if e["type"] == "team.ticket.deleted"]
    assert deleted[0]["actor"] == "t-dev"


def test_unknown_task_raises_lane_error(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with pytest.raises(LaneError, match="Task not found"):
        archive_ticket(home, home / "tasks", home / "logs", home / "teams", "task_nope")


# --- the CLI ----------------------------------------------------------------


def test_cli_archive_reports_where_the_file_went(tmp_path: Path, capsys) -> None:
    home = _home(tmp_path)
    task = _ticket(home, "backlog")
    assert main(["--home", str(home), "task", "archive", task.id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "archive"
    assert out["archived_to"].endswith(f"archive/{task.id}.json")
    assert _archived(home, task.id).exists()


def test_cli_delete_promises_no_archive(tmp_path: Path, capsys) -> None:
    home = _home(tmp_path)
    task = _ticket(home, "backlog")
    assert main(["--home", str(home), "task", "delete", task.id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "delete"
    assert "archived_to" not in out          # nothing to point at
    assert not _archived(home, task.id).exists()


@pytest.mark.parametrize("command", ["archive", "delete"])
def test_cli_gate_refusal_exits_nonzero(tmp_path: Path, capsys, command: str) -> None:
    home = _home(tmp_path)
    task = _ticket(home, "testing")
    assert main(["--home", str(home), "task", command, task.id, "--as", "t-dev"]) == 1
    assert "gated by" in capsys.readouterr().out
    assert find_task(home / "tasks", task.id) is not None

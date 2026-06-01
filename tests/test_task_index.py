from __future__ import annotations

from pathlib import Path

from jigga.core.io import read_json
from jigga.runtime.tasks import (
    _index_path,
    create_task,
    find_task,
    forget_tasks,
    pending_summary,
    rebuild_index,
    set_task_state,
    tasks_for_agent,
    write_task,
)


def test_create_maintains_index(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    t = create_task(tasks, "do a thing", assignee="alpha")
    index = read_json(_index_path(tasks))
    assert index[t.id] == {"state": "pending", "assignee": "alpha", "created_at": t.created_at}


def test_index_rebuilds_when_missing(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    a = create_task(tasks, "a", assignee="alpha")
    b = create_task(tasks, "b", assignee="beta")
    # nuke the index — readers must self-heal from the task files
    _index_path(tasks).unlink()
    rebuilt = rebuild_index(tasks)
    assert set(rebuilt) == {a.id, b.id}
    assert [t.id for t in tasks_for_agent(tasks, "alpha")] == [a.id]


def test_state_change_reflected_in_pending_summary(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    a = create_task(tasks, "a", assignee="alpha")
    create_task(tasks, "b", assignee="beta")
    targets, count = pending_summary(tasks)
    assert targets == ["alpha", "beta"] and count == 2
    set_task_state(tasks, a.id, "completed")
    targets, count = pending_summary(tasks)
    assert targets == ["beta"] and count == 1
    assert tasks_for_agent(tasks, "alpha") == []  # no longer pending


def test_find_task_by_prefix_via_index(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    t = create_task(tasks, "a", assignee="alpha")
    found = find_task(tasks, t.id[:8])
    assert found is not None and found.id == t.id
    assert find_task(tasks, "task_nonexistent") is None


def test_forget_drops_index_entry(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    t = create_task(tasks, "a", assignee="alpha")
    forget_tasks(tasks, [f"{t.id}.json"])
    assert t.id not in read_json(_index_path(tasks))


def test_reader_tolerates_archived_file(tmp_path: Path) -> None:
    # index entry whose file has been moved out (e.g. compaction) must not blow up
    tasks = tmp_path / "tasks"
    t = create_task(tasks, "a", assignee="alpha")
    (tasks / f"{t.id}.json").unlink()  # file gone, index still references it
    assert find_task(tasks, t.id) is None
    assert tasks_for_agent(tasks, "alpha") == []


def test_pending_summary_reflects_the_index_not_disk(tmp_path: Path) -> None:
    """Proves the index is actually consulted: a hand-edited index that disagrees
    with the on-disk task changes the answer (so pending_summary/tasks_for_agent
    read the index, not a disk glob)."""
    from jigga.core.io import write_json
    tasks = tmp_path / "tasks"
    t = create_task(tasks, "a", assignee="alpha")          # file: pending, index: pending
    # diverge the index: mark it completed while the task file stays pending
    idx = read_json(_index_path(tasks))
    idx[t.id]["state"] = "completed"
    write_json(_index_path(tasks), idx)

    targets, count = pending_summary(tasks)
    assert targets == [] and count == 0                    # reflects the index, not the pending file
    assert tasks_for_agent(tasks, "alpha") == []


def test_find_task_prefix_tie_breaks_on_created_at(tmp_path: Path) -> None:
    from jigga.core.models import Task
    tasks = tmp_path / "tasks"
    write_task(tasks, Task(id="task_dup_late", title="late", created_at="2020-01-02T00:00:00+00:00"))
    write_task(tasks, Task(id="task_dup_early", title="early", created_at="2020-01-01T00:00:00+00:00"))
    found = find_task(tasks, "task_dup")                    # shared prefix → earliest created wins
    assert found is not None and found.id == "task_dup_early"

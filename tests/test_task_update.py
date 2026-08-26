"""`jigga task update` — editing a ticket after it is filed.

Before this, the task store could create, list, set-state and move, but nothing
could change a title, description or assignee. A typo in a hand-filed ticket was
permanent, which made the dashboard's ticket board read-only in practice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jigga.runtime.tasks import create_task, find_task, update_task


def _task(tmp_path: Path, **kwargs):
    return create_task(tmp_path, kwargs.pop("title", "Original"), **kwargs)


def test_update_changes_only_the_fields_passed(tmp_path: Path) -> None:
    task = _task(tmp_path, description="keep me", assignee="dev")
    updated = update_task(tmp_path, task.id, title="New title")
    assert updated.title == "New title"
    assert updated.description == "keep me"      # untouched
    assert updated.assignee == "dev"             # untouched


def test_update_persists_to_disk(tmp_path: Path) -> None:
    task = _task(tmp_path)
    update_task(tmp_path, task.id, title="Persisted")
    assert find_task(tmp_path, task.id).title == "Persisted"


def test_update_bumps_updated_at(tmp_path: Path) -> None:
    task = _task(tmp_path)
    before = task.updated_at
    updated = update_task(tmp_path, task.id, description="something")
    assert updated.updated_at >= before


def test_description_can_be_cleared_but_absent_means_unchanged(tmp_path: Path) -> None:
    task = _task(tmp_path, description="original")
    assert update_task(tmp_path, task.id, title="t").description == "original"
    assert update_task(tmp_path, task.id, description=None).description is None


def test_assignee_cleared_by_empty_string(tmp_path: Path) -> None:
    task = _task(tmp_path, assignee="dev")
    assert update_task(tmp_path, task.id, assignee="").assignee is None
    assert update_task(tmp_path, task.id, assignee="  qa  ").assignee == "qa"


def test_empty_title_is_refused(tmp_path: Path) -> None:
    task = _task(tmp_path)
    with pytest.raises(ValueError, match="Title cannot be empty"):
        update_task(tmp_path, task.id, title="   ")
    assert find_task(tmp_path, task.id).title == "Original"   # unchanged on failure


def test_unknown_task_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Task not found"):
        update_task(tmp_path, "task_nope", title="x")


def test_update_does_not_touch_state_or_lane(tmp_path: Path) -> None:
    # state/lane have their own gated entry points; a generic setter must not
    # become a way around a lane's gate.
    task = create_task(tmp_path, "T", lane="testing", metadata={"team_id": "t"})
    updated = update_task(tmp_path, task.id, title="renamed")
    assert updated.lane == "testing"
    assert updated.state == task.state


def test_update_leaves_state_lane_metadata_alone_when_omitted(tmp_path: Path) -> None:
    # Same guarantee as above, extended to the new keyword-only fields: a
    # caller that never mentions them must not have them change.
    task = create_task(tmp_path, "T", lane="testing", metadata={"team_id": "t"})
    updated = update_task(tmp_path, task.id, title="renamed")
    assert updated.lane == "testing"
    assert updated.state == task.state
    assert updated.metadata == {"team_id": "t"}


def test_update_can_set_state_lane_assignee_metadata_together(tmp_path: Path) -> None:
    # The ticket-outcome path (runtime/agent.py) writes all four fields from
    # one already-resolved outcome in a single call.
    task = create_task(tmp_path, "T", assignee="dev", lane="testing",
                       metadata={"team_id": "t", "bounces": 0})
    updated = update_task(tmp_path, task.id, state="pending", lane="backlog",
                          assignee="lead", metadata={"team_id": "t", "bounces": 1})
    assert updated.state == "pending"
    assert updated.lane == "backlog"
    assert updated.assignee == "lead"
    assert updated.metadata == {"team_id": "t", "bounces": 1}
    persisted = find_task(tmp_path, task.id)
    assert persisted.state == "pending"
    assert persisted.lane == "backlog"
    assert persisted.assignee == "lead"
    assert persisted.metadata == {"team_id": "t", "bounces": 1}


def test_update_lane_can_be_cleared_to_none(tmp_path: Path) -> None:
    task = create_task(tmp_path, "T", lane="testing", metadata={"team_id": "t"})
    updated = update_task(tmp_path, task.id, lane=None)
    assert updated.lane is None


def test_update_state_validates(tmp_path: Path) -> None:
    task = create_task(tmp_path, "T")
    with pytest.raises(ValueError, match="Invalid task state"):
        update_task(tmp_path, task.id, state="not-a-real-state")

"""`waiting` means "this ticket is waiting on its children".

It is deliberately not `blocked`: blocked means "bounced too often, a human must
look", and overloading it would make a healthy epic indistinguishable from a
stuck ticket. It is deliberately not `pending`: the supervisor selects pending
tasks, so a waiting epic would be woken every tick, bounce, and block itself
while its stories were still being built.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.models import validate_task_state
from jigga.runtime.tasks import create_task, set_task_state, tasks_for_agent


def test_waiting_is_a_valid_task_state() -> None:
    assert validate_task_state("waiting") == "waiting"


def test_an_invalid_state_is_still_rejected() -> None:
    with pytest.raises(ValueError):
        validate_task_state("dawdling")


def test_the_supervisor_does_not_pick_up_a_waiting_ticket(tmp_path: Path) -> None:
    """The whole reason for the state: a waiting epic must not be woken."""
    paths = init_runtime(tmp_path)
    task = create_task(paths.tasks, "epic", assignee="eng-lead", lane="in-progress")
    set_task_state(paths.tasks, task.id, "waiting")

    assert [t.id for t in tasks_for_agent(paths.tasks, "eng-lead")] == []

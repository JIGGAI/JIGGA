# tests/test_decompose_release.py
"""An epic wakes exactly once: when its stories are finished, or when one dies.

A failed child must release it immediately. Waiting for a story that will never
complete would park the epic forever — the silent stall this whole line of work
exists to remove.
"""
from __future__ import annotations

from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.decompose import decompose, release_parent_if_ready
from jigga.runtime.tasks import create_task, find_task, set_task_state

PIPELINE = [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
            {"id": "ready-for-pr"}, {"id": "done"}]
TRANSITIONS = {
    "rules": [
        {"from": "lead", "to": "dev", "lane": "in-progress"},
        {"from": "dev", "to": "test", "lane": "testing"},
        {"from": "test", "to": "dev", "lane": "in-progress"},
        {"from": "test", "to": "lead", "lane": "ready-for-pr"},
    ],
    "bounce_lane": "backlog",
}
ROSTER = [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"},
          {"id": "eng-test", "role": "test"}]
STORIES = [{"title": "one", "description": "brief", "assignee": "eng-dev"},
           {"title": "two", "description": "brief", "assignee": "eng-dev"}]


def _setup(tmp_path: Path):
    paths = init_runtime(tmp_path)
    write_yaml(paths.teams / "eng.yaml", {"id": "eng", "name": "Eng", "agents": ROSTER,
                                          "lanes": PIPELINE, "lane_transitions": TRANSITIONS})
    epic = create_task(paths.tasks, "New website", description="A website.",
                       assignee="eng-lead", lane="backlog", metadata={"team_id": "eng"})
    result = decompose(paths.tasks, paths.teams, ticket_id=epic.id, actor="eng-lead",
                       summary="Cut by surface.", plan="plans/x.md", stories=STORIES)
    return paths, epic.id, result["stories"]


def test_the_epic_stays_asleep_until_the_last_child_is_done(tmp_path: Path) -> None:
    paths, epic_id, kids = _setup(tmp_path)

    set_task_state(paths.tasks, kids[0], "completed")
    assert release_parent_if_ready(paths.tasks, paths.teams, kids[0]) is None
    assert find_task(paths.tasks, epic_id).state == "waiting"


def test_the_last_child_releases_it_to_the_lead_in_the_close_lane(tmp_path: Path) -> None:
    """The close lane specifically: tickets.close refuses anything outside it,
    so an epic released into in-progress could never be closed at all."""
    paths, epic_id, kids = _setup(tmp_path)
    for kid in kids:
        set_task_state(paths.tasks, kid, "completed")

    released = release_parent_if_ready(paths.tasks, paths.teams, kids[-1])

    assert released == {"epic": epic_id, "reason": "children complete"}
    epic = find_task(paths.tasks, epic_id)
    assert epic.state == "pending"
    assert epic.lane == "ready-for-pr"
    assert epic.assignee == "eng-lead"


def test_a_failed_child_releases_the_epic_at_once(tmp_path: Path) -> None:
    paths, epic_id, kids = _setup(tmp_path)

    set_task_state(paths.tasks, kids[0], "failed")
    released = release_parent_if_ready(paths.tasks, paths.teams, kids[0])

    assert released is not None
    assert kids[0] in released["reason"]
    epic = find_task(paths.tasks, epic_id)
    assert epic.state == "pending"
    assert epic.assignee == "eng-lead"


def test_a_blocked_child_releases_the_epic_at_once(tmp_path: Path) -> None:
    paths, epic_id, kids = _setup(tmp_path)
    set_task_state(paths.tasks, kids[1], "blocked")
    assert release_parent_if_ready(paths.tasks, paths.teams, kids[1]) is not None
    assert find_task(paths.tasks, epic_id).state == "pending"


def test_a_task_with_no_parent_releases_nothing(tmp_path: Path) -> None:
    paths, _epic_id, _kids = _setup(tmp_path)
    orphan = create_task(paths.tasks, "orphan", assignee="eng-dev", lane="backlog",
                         metadata={"team_id": "eng"})
    assert release_parent_if_ready(paths.tasks, paths.teams, orphan.id) is None


def test_releasing_twice_is_harmless(tmp_path: Path) -> None:
    """The runtime calls this on every child completion; it must be idempotent."""
    paths, epic_id, kids = _setup(tmp_path)
    for kid in kids:
        set_task_state(paths.tasks, kid, "completed")
    assert release_parent_if_ready(paths.tasks, paths.teams, kids[-1]) is not None
    assert release_parent_if_ready(paths.tasks, paths.teams, kids[-1]) is None
    assert find_task(paths.tasks, epic_id).state == "pending"

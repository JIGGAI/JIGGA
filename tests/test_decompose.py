"""A lead breaks one complex ask into linked stories.

Before this, the lead's only options were tickets.handoff (give the whole thing
to one agent) or task.assign (create an unrelated ticket, and now refused while
holding a lane-managed ticket). "Build a new website" went to one dev as a
single ticket.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.decompose import DecomposeError, decompose
from jigga.runtime.tasks import create_task, find_task, list_tasks

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

STORIES = [
    {"title": "Scaffold the app", "description": "Full brief with an acceptance check.",
     "assignee": "eng-dev"},
    {"title": "Build the nav", "description": "Another full brief.", "assignee": "eng-dev"},
]


def _setup(tmp_path: Path, lanes=PIPELINE, transitions=TRANSITIONS, agents=ROSTER):
    paths = init_runtime(tmp_path)
    data = {"id": "eng", "name": "Eng", "agents": agents, "lanes": lanes}
    if transitions is not None:
        data["lane_transitions"] = transitions
    write_yaml(paths.teams / "eng.yaml", data)
    return paths


def _epic(paths, lane="backlog"):
    return create_task(paths.tasks, "New website", description="## Requirements\nA website.",
                       assignee="eng-lead", lane=lane, metadata={"team_id": "eng"})


def _run(paths, epic_id, actor="eng-lead", stories=None, summary="Cut by surface.",
         plan="shared-context/plans/new-website.md"):
    return decompose(paths.tasks, paths.teams, ticket_id=epic_id, actor=actor,
                     summary=summary, plan=plan, stories=stories or STORIES)


def test_each_story_becomes_a_ticket_linked_to_the_epic(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = _epic(paths)

    result = _run(paths, epic.id)

    assert len(result["stories"]) == 2
    for sid, spec in zip(result["stories"], STORIES):
        story = find_task(paths.tasks, sid)
        assert story.title == spec["title"]
        assert story.description == spec["description"]
        assert story.assignee == spec["assignee"]
        assert story.state == "pending"
        assert story.metadata["parent"] == epic.id
        assert story.metadata["team_id"] == "eng"


def test_stories_start_in_the_first_lane_not_handed_off(tmp_path: Path) -> None:
    """Decomposition creates work; moving it is the board's job."""
    paths = _setup(tmp_path)
    epic = _epic(paths)
    result = _run(paths, epic.id)
    assert {find_task(paths.tasks, s).lane for s in result["stories"]} == {"backlog"}


def test_the_epic_waits_in_the_derived_work_lane(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = _epic(paths)

    result = _run(paths, epic.id)

    fresh = find_task(paths.tasks, epic.id)
    assert fresh.state == "waiting"
    assert fresh.lane == "in-progress"          # derived from the lead->dev rule
    assert result["lane"] == "in-progress"
    assert fresh.metadata["children"] == result["stories"]
    assert fresh.metadata["plan"] == "shared-context/plans/new-website.md"


def test_an_underivable_work_lane_leaves_the_epic_where_it_is(tmp_path: Path) -> None:
    """Core must not invent a lane. A team with no lead->dev rule keeps its epic
    in place rather than being handed a column it never declared."""
    paths = _setup(tmp_path, transitions={"rules": [{"from": "dev", "to": "test",
                                                     "lane": "testing"}],
                                          "bounce_lane": "backlog"})
    epic = _epic(paths)
    result = _run(paths, epic.id)
    assert find_task(paths.tasks, epic.id).lane == "backlog"
    assert result["lane"] is None


def test_only_the_lead_may_decompose(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = _epic(paths)
    with pytest.raises(DecomposeError, match="lead"):
        _run(paths, epic.id, actor="eng-dev")
    assert len(list_tasks(paths.tasks)) == 1


def test_every_story_needs_a_brief(tmp_path: Path) -> None:
    """A story without a description is the six-word-ticket bug again."""
    paths = _setup(tmp_path)
    epic = _epic(paths)
    with pytest.raises(DecomposeError, match="description"):
        _run(paths, epic.id, stories=[{"title": "vague", "assignee": "eng-dev"}])
    assert len(list_tasks(paths.tasks)) == 1


def test_a_summary_is_required(tmp_path: Path) -> None:
    """A bare path makes the board unreadable without a second lookup."""
    paths = _setup(tmp_path)
    epic = _epic(paths)
    with pytest.raises(DecomposeError, match="summary"):
        _run(paths, epic.id, summary="   ")


def test_decomposing_twice_is_refused(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = _epic(paths)
    _run(paths, epic.id)
    with pytest.raises(DecomposeError, match="already"):
        _run(paths, epic.id)
    assert len(list_tasks(paths.tasks)) == 3      # epic + 2, not 5


def test_the_story_cap_is_enforced(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = _epic(paths)
    many = [{"title": f"s{i}", "description": "brief", "assignee": "eng-dev"} for i in range(21)]
    with pytest.raises(DecomposeError, match="20"):
        _run(paths, epic.id, stories=many)
    assert len(list_tasks(paths.tasks)) == 1


def test_a_story_assignee_must_be_on_the_team(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = _epic(paths)
    with pytest.raises(DecomposeError, match="stranger"):
        _run(paths, epic.id, stories=[{"title": "s", "description": "b", "assignee": "stranger"}])


def test_a_non_lifecycle_team_cannot_decompose(tmp_path: Path) -> None:
    paths = _setup(tmp_path, transitions=None)
    epic = _epic(paths)
    with pytest.raises(DecomposeError, match="board"):
        _run(paths, epic.id)


def test_the_work_lane_derives_from_a_non_dev_builder_role(tmp_path: Path) -> None:
    """The builder role name is not hardcoded to "dev" — it's read off the
    lead's own transition rule, so a team that calls it "builder" (or
    "engineer", or anything else) still gets its epic parked correctly."""
    agents = [{"id": "eng-lead", "role": "lead"}, {"id": "eng-builder", "role": "builder"},
              {"id": "eng-test", "role": "test"}]
    transitions = {
        "rules": [
            {"from": "lead", "to": "builder", "lane": "in-progress"},
            {"from": "builder", "to": "test", "lane": "testing"},
            {"from": "test", "to": "builder", "lane": "in-progress"},
            {"from": "test", "to": "lead", "lane": "ready-for-pr"},
        ],
        "bounce_lane": "backlog",
    }
    stories = [{"title": "Scaffold the app", "description": "Full brief.",
                "assignee": "eng-builder"}]
    paths = _setup(tmp_path, transitions=transitions, agents=agents)
    epic = _epic(paths)

    result = _run(paths, epic.id, stories=stories)

    assert find_task(paths.tasks, epic.id).lane == "in-progress"
    assert result["lane"] == "in-progress"


def test_a_bad_story_partway_through_the_list_creates_nothing(tmp_path: Path) -> None:
    """Validation runs to completion before any ticket is created, so a
    failure on story N does not leave stories 1..N-1 as orphans with no
    parent to point back to."""
    paths = _setup(tmp_path)
    epic = _epic(paths)
    stories = [
        {"title": "Valid first story", "description": "Full brief.", "assignee": "eng-dev"},
        {"title": "Missing its description", "assignee": "eng-dev"},
    ]
    with pytest.raises(DecomposeError, match="description"):
        _run(paths, epic.id, stories=stories)
    assert len(list_tasks(paths.tasks)) == 1

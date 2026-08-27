"""One epic, three stories, one lap.

Proves the pieces compose: the epic sleeps while its stories are built, wakes
exactly once when the last finishes, and closes through the ordinary path.
"""
from __future__ import annotations

from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import TeamConfig
from jigga.runtime.decompose import decompose, release_parent_if_ready
from jigga.runtime.lanes import render_lanes
from jigga.runtime.tasks import create_task, find_task, list_tasks, set_task_state

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


def test_the_board_says_when_to_decompose() -> None:
    team = TeamConfig.from_dict({"id": "eng", "name": "Eng", "agents": ROSTER,
                                 "lanes": PIPELINE, "lane_transitions": TRANSITIONS})
    text = render_lanes(team)
    assert "tickets.decompose" in text
    assert "tickets.handoff" in text, "both verbs, so the choice is visible"


def test_an_epic_sleeps_through_its_stories_and_wakes_once(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    write_yaml(paths.teams / "eng.yaml", {"id": "eng", "name": "Eng", "agents": ROSTER,
                                          "lanes": PIPELINE, "lane_transitions": TRANSITIONS})
    epic = create_task(paths.tasks, "New website", description="A website.",
                       assignee="eng-lead", lane="backlog", metadata={"team_id": "eng"})

    result = decompose(paths.tasks, paths.teams, ticket_id=epic.id, actor="eng-lead",
                       summary="Cut by surface.", plan="shared-context/plans/site.md",
                       stories=[{"title": f"story {i}", "description": "brief",
                                 "assignee": "eng-dev"} for i in range(3)])

    # The board shows the ask plus its pieces, and the ask is readable.
    assert len(list_tasks(paths.tasks)) == 4
    text = find_task(paths.tasks, epic.id).description
    assert "Cut by surface." in text
    assert "shared-context/plans/site.md" in text
    assert "Original request" in text

    # It sleeps while the first two are built.
    for kid in result["stories"][:2]:
        set_task_state(paths.tasks, kid, "completed")
        assert release_parent_if_ready(paths.tasks, paths.teams, kid) is None
        assert find_task(paths.tasks, epic.id).state == "waiting"

    # ...and wakes on the last, in the lane the lead can close from.
    set_task_state(paths.tasks, result["stories"][2], "completed")
    assert release_parent_if_ready(paths.tasks, paths.teams, result["stories"][2]) is not None
    epic_now = find_task(paths.tasks, epic.id)
    assert (epic_now.state, epic_now.lane, epic_now.assignee) == ("pending", "ready-for-pr", "eng-lead")

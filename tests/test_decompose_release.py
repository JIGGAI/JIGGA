# tests/test_decompose_release.py
"""An epic wakes exactly once: when its stories are finished, or when one dies.

A failed child must release it immediately. Waiting for a story that will never
complete would park the epic forever — the silent stall this whole line of work
exists to remove.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import read_json, write_json, write_yaml
from jigga.runtime.decompose import decompose, release_parent_if_ready
from jigga.runtime.tasks import (
    archive_task,
    create_task,
    find_task,
    set_task_state,
    update_task,
)

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


def _events(paths, event_type: str) -> list[dict]:
    path = paths.logs / "events.jsonl"
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [event for event in events if event["type"] == event_type]


def _write_agents(paths) -> None:
    for aid in ("eng-lead", "eng-dev"):
        write_yaml(paths.agents / f"{aid}.yaml", {
            "id": aid, "name": aid, "role": "r", "memory_scope": "task_only",
            "tools": [], "permissions": {}, "permission_mode": "autonomous"})


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


def test_a_gone_child_releases_the_epic_instead_of_parking_it_forever(tmp_path: Path) -> None:
    """An archived (or deleted) child fires no future completion event. Treating
    it as neither dead nor complete would park the epic forever — the exact
    stall this function exists to remove."""
    paths, epic_id, kids = _setup(tmp_path)
    archive_task(paths.tasks, kids[0])
    set_task_state(paths.tasks, kids[1], "completed")

    released = release_parent_if_ready(paths.tasks, paths.teams, kids[1])

    assert released is not None
    assert kids[0] in released["reason"]
    epic = find_task(paths.tasks, epic_id)
    assert epic.state == "pending"
    assert epic.assignee == "eng-lead"


def test_releasing_twice_is_harmless(tmp_path: Path) -> None:
    """The runtime calls this on every child completion; it must be idempotent."""
    paths, epic_id, kids = _setup(tmp_path)
    for kid in kids:
        set_task_state(paths.tasks, kid, "completed")
    assert release_parent_if_ready(paths.tasks, paths.teams, kids[-1]) is not None
    assert release_parent_if_ready(paths.tasks, paths.teams, kids[-1]) is None
    assert find_task(paths.tasks, epic_id).state == "pending"


def test_a_child_completing_through_a_real_run_releases_the_epic(tmp_path: Path) -> None:
    """The wiring, not just the function: a story closing during an agent run
    must wake its epic without anyone calling the helper by hand."""
    from unittest.mock import patch

    from jigga.runtime.agent import run_agent
    from jigga.runtime.model_router import ModelCallResult

    paths, epic_id, kids = _setup(tmp_path)
    for aid in ("eng-lead", "eng-dev"):
        write_yaml(paths.agents / f"{aid}.yaml", {
            "id": aid, "name": aid, "role": "r", "memory_scope": "task_only",
            "tools": [], "permissions": {}, "permission_mode": "autonomous"})
    # First story already done; the second finishes in this run.
    set_task_state(paths.tasks, kids[0], "completed")
    from jigga.runtime.tasks import update_task
    update_task(paths.tasks, kids[1], lane="done")

    result = ModelCallResult(status="ok", provider="dry_run", model="m",
                             content="done", dry_run=True, tool_calls=[])
    with patch("jigga.runtime.agent.call_model", lambda *a, **k: result):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    epic = find_task(paths.tasks, epic_id)
    assert epic.state == "pending", "the epic should have been woken by the run"
    assert epic.lane == "ready-for-pr"
    released = _events(paths, "ticket.epic.released")
    assert [e["details"]["reason"] for e in released] == ["children complete"]
    assert released[0]["details"]["child_state"] == "completed"


def _ok_result():
    from jigga.runtime.model_router import ModelCallResult
    return ModelCallResult(status="ok", provider="dry_run", model="m", content="done",
                           dry_run=True, tool_calls=[])


def test_a_child_failing_in_a_real_run_releases_the_epic(tmp_path: Path) -> None:
    """The Critical. A failed run writes `failed` onto the story and NOTHING
    moves that ticket again — `tasks_for_agent` selects only `pending` and the
    stale sweep only `claimed`/`running`. Gated on `completed`, the release
    never fired for the last (or only) story and the epic parked forever."""
    from unittest.mock import patch

    from jigga.runtime.agent import run_agent
    from jigga.runtime.model_router import ModelCallResult

    paths, epic_id, kids = _setup(tmp_path)
    _write_agents(paths)
    set_task_state(paths.tasks, kids[0], "completed")   # the last story is the one that dies

    boom = ModelCallResult(status="error", provider="dry_run", model="m", content="",
                           dry_run=True, tool_calls=[], error="provider exploded")
    with patch("jigga.runtime.agent.call_model", lambda *a, **k: boom):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    assert find_task(paths.tasks, kids[1]).state == "failed"
    epic = find_task(paths.tasks, epic_id)
    assert epic.state == "pending", "a dead story must wake the epic, not park it"
    assert epic.lane == "ready-for-pr"
    assert epic.assignee == "eng-lead"

    released = _events(paths, "ticket.epic.released")
    assert len(released) == 1, "the wake must be in the audit log"
    details = released[0]["details"]
    assert details["task_id"] == epic_id
    assert details["child"] == kids[1]
    assert details["child_state"] == "failed"
    assert kids[1] in details["reason"] and "failed" in details["reason"]


def test_the_only_story_failing_still_releases_the_epic(tmp_path: Path) -> None:
    """No sibling completes afterwards to carry the release, so this is the
    case the old gate could never recover from."""
    from unittest.mock import patch

    from jigga.runtime.agent import run_agent
    from jigga.runtime.model_router import ModelCallResult

    paths = init_runtime(tmp_path)
    write_yaml(paths.teams / "eng.yaml", {"id": "eng", "name": "Eng", "agents": ROSTER,
                                          "lanes": PIPELINE, "lane_transitions": TRANSITIONS})
    _write_agents(paths)
    epic = create_task(paths.tasks, "One thing", description="x.", assignee="eng-lead",
                       lane="backlog", metadata={"team_id": "eng"})
    decompose(paths.tasks, paths.teams, ticket_id=epic.id, actor="eng-lead",
              summary="One story.", plan="plans/x.md",
              stories=[{"title": "only", "description": "brief", "assignee": "eng-dev"}])
    assert find_task(paths.tasks, epic.id).state == "waiting"

    boom = ModelCallResult(status="error", provider="dry_run", model="m", content="",
                           dry_run=True, tool_calls=[], error="provider exploded")
    with patch("jigga.runtime.agent.call_model", lambda *a, **k: boom):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    assert find_task(paths.tasks, epic.id).state == "pending"
    assert len(_events(paths, "ticket.epic.released")) == 1


def test_a_child_blocked_past_max_bounces_in_a_real_run_releases_the_epic(tmp_path: Path) -> None:
    """The second producer of a dead child: a story that has bounced too often
    is written `blocked` by the outcome rule and never runs again."""
    from unittest.mock import patch

    from jigga.runtime.agent import run_agent
    from jigga.runtime.ticket_outcome import MAX_BOUNCES

    paths, epic_id, kids = _setup(tmp_path)
    _write_agents(paths)
    set_task_state(paths.tasks, kids[0], "completed")
    doomed = find_task(paths.tasks, kids[1])
    metadata = dict(doomed.metadata or {})
    metadata["bounces"] = MAX_BOUNCES
    update_task(paths.tasks, kids[1], metadata=metadata)

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _ok_result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    assert find_task(paths.tasks, kids[1]).state == "blocked"
    epic = find_task(paths.tasks, epic_id)
    assert epic.state == "pending"
    assert epic.lane == "ready-for-pr"
    released = _events(paths, "ticket.epic.released")
    assert len(released) == 1
    assert released[0]["details"]["child_state"] == "blocked"


def test_the_stale_sweeper_releases_the_epic_of_the_story_it_kills(tmp_path: Path) -> None:
    """The third producer. `sweep_stale` writes `failed` outside any run, so
    without its own release call a crashed story parks its epic forever."""
    from jigga.runtime.recovery import sweep_stale

    paths, epic_id, kids = _setup(tmp_path)
    set_task_state(paths.tasks, kids[0], "completed")
    set_task_state(paths.tasks, kids[1], "claimed")
    set_task_state(paths.tasks, kids[1], "running")
    record = read_json(paths.tasks / f"{kids[1]}.json")
    record["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    write_json(paths.tasks / f"{kids[1]}.json", record)

    assert sweep_stale(paths)["tasks"] == [kids[1]]

    epic = find_task(paths.tasks, epic_id)
    assert epic.state == "pending", "the sweeper must wake the epic it just orphaned"
    assert epic.lane == "ready-for-pr"
    assert epic.assignee == "eng-lead"
    released = _events(paths, "ticket.epic.released")
    assert len(released) == 1
    assert released[0]["details"]["child_state"] == "failed"


def test_the_sweeper_leaves_a_parentless_task_alone(tmp_path: Path) -> None:
    from jigga.runtime.recovery import sweep_stale

    paths, _epic_id, _kids = _setup(tmp_path)
    orphan = create_task(paths.tasks, "orphan", assignee="eng-dev", lane="backlog",
                         metadata={"team_id": "eng"})
    set_task_state(paths.tasks, orphan.id, "running")
    record = read_json(paths.tasks / f"{orphan.id}.json")
    record["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    write_json(paths.tasks / f"{orphan.id}.json", record)

    assert sweep_stale(paths)["tasks"] == [orphan.id]
    assert _events(paths, "ticket.epic.released") == []

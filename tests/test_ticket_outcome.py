from __future__ import annotations

from jigga.core.models import Task, TeamConfig
from jigga.runtime.ticket_outcome import resolve_ticket_outcome

TEAM = TeamConfig.from_dict({
    "id": "eng", "name": "Eng",
    "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"}],
    "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
              {"id": "ready-for-pr"}, {"id": "done"}],
})


def _ticket(**kw) -> Task:
    base = {"id": "task_1", "title": "t", "assignee": "eng-dev", "lane": "in-progress"}
    base.update(kw)
    return Task(**base)


def test_a_finished_run_does_not_complete_the_ticket() -> None:
    # The whole point: finishing a run is not finishing the work.
    out = resolve_ticket_outcome(_ticket(), TEAM, run_state="completed")
    assert out["state"] != "completed"


def test_an_unhandled_ticket_bounces_to_the_lead() -> None:
    out = resolve_ticket_outcome(_ticket(), TEAM, run_state="completed")
    assert out == {"state": "pending", "lane": "backlog", "assignee": "eng-lead", "bounced": True}


def test_a_ticket_in_done_completes() -> None:
    out = resolve_ticket_outcome(_ticket(lane="done"), TEAM, run_state="completed")
    assert out["state"] == "completed"
    assert out["bounced"] is False


def test_a_failed_run_fails_the_ticket_and_moves_nothing() -> None:
    out = resolve_ticket_outcome(_ticket(), TEAM, run_state="failed")
    assert out == {"state": "failed", "lane": "in-progress", "assignee": "eng-dev", "bounced": False}


def test_an_approval_park_is_left_alone() -> None:
    out = resolve_ticket_outcome(_ticket(), TEAM, run_state="needs_approval")
    assert out["state"] == "needs_approval"
    assert out["bounced"] is False


def test_a_reassigned_ticket_is_not_a_bounce() -> None:
    # The agent handed it on during the run; the ticket already moved.
    ticket = _ticket(assignee="eng-test", lane="testing")
    out = resolve_ticket_outcome(ticket, TEAM, run_state="completed", ran_as="eng-dev")
    assert out == {"state": "pending", "lane": "testing", "assignee": "eng-test", "bounced": False}


def test_the_bounce_guard_blocks_after_three() -> None:
    ticket = _ticket(metadata={"bounces": 3})
    out = resolve_ticket_outcome(ticket, TEAM, run_state="completed")
    assert out["state"] == "blocked"


def test_a_team_without_a_bounce_lane_blocks_instead() -> None:
    team = TeamConfig.from_dict({"id": "x", "name": "X", "agents": [],
                                 "lanes": [{"id": "a"}, {"id": "done"}]})
    out = resolve_ticket_outcome(_ticket(lane="a"), team, run_state="completed")
    assert out["state"] == "blocked"

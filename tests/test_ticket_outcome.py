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


def test_an_unhandled_ticket_with_no_running_agent_bounces_to_the_lead() -> None:
    # No `ran_as` means nothing to advance from.
    out = resolve_ticket_outcome(_ticket(), TEAM, run_state="completed")
    assert out == {"state": "pending", "lane": "backlog", "assignee": "eng-lead",
                   "bounced": True, "advanced": False}


def test_a_dev_that_finishes_without_handing_off_advances_to_qa() -> None:
    """The ordinary case this change exists for.

    A push pipeline only moves when the holder hands the ticket on, and agents
    kept not doing it — the work stopped and the ticket bounced until it
    blocked. dev has exactly one outgoing transition, so there is no judgment
    to make and the runtime makes the move.
    """
    full = TeamConfig.from_dict({
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"},
                   {"id": "eng-test", "role": "test"}],
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
                  {"id": "ready-for-pr"}, {"id": "done"}],
    })
    out = resolve_ticket_outcome(_ticket(assignee="eng-dev", lane="in-progress"),
                                 full, run_state="completed", ran_as="eng-dev")
    assert out == {"state": "pending", "lane": "testing", "assignee": "eng-test",
                   "bounced": False, "advanced": True}


def test_a_ticket_in_done_completes() -> None:
    out = resolve_ticket_outcome(_ticket(lane="done"), TEAM, run_state="completed")
    assert out["state"] == "completed"
    assert out["bounced"] is False


def test_a_ticket_waiting_in_the_close_lane_does_not_bounce() -> None:
    # QA passed; it sits in ready-for-pr until the lead confirms the merge and
    # closes it. The lead's first run without a merge must not send finished
    # work back to backlog — three of those would block it.
    ticket = _ticket(lane="ready-for-pr", assignee="eng-lead")
    out = resolve_ticket_outcome(ticket, TEAM, run_state="completed", ran_as="eng-lead")
    assert out == {"state": "pending", "lane": "ready-for-pr",
                   "assignee": "eng-lead", "bounced": False, "advanced": False}


def test_waiting_in_the_close_lane_never_reaches_the_bounce_guard() -> None:
    # Even at the ceiling: waiting is not looping, so it must not block either.
    ticket = _ticket(lane="ready-for-pr", assignee="eng-lead", metadata={"bounces": 3})
    out = resolve_ticket_outcome(ticket, TEAM, run_state="completed", ran_as="eng-lead")
    assert out["state"] == "pending"


def test_the_close_lane_follows_the_teams_own_rules() -> None:
    team = TeamConfig.from_dict({
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"}],
        "lanes": [{"id": "backlog"}, {"id": "building"}, {"id": "awaiting-merge"}, {"id": "done"}],
        "lane_transitions": {"rules": [{"from": "dev", "to": "lead", "lane": "awaiting-merge"}],
                             "bounce_lane": "backlog"},
    })
    waiting = resolve_ticket_outcome(_ticket(lane="awaiting-merge", assignee="eng-lead"),
                                     team, run_state="completed", ran_as="eng-lead")
    assert waiting["state"] == "pending" and waiting["bounced"] is False
    # ready-for-pr is not this board's close lane, so it has no special standing.
    other = resolve_ticket_outcome(_ticket(lane="building", assignee="eng-lead"),
                                   team, run_state="completed", ran_as="eng-lead")
    assert other["bounced"] is True


def test_a_failed_run_fails_the_ticket_and_moves_nothing() -> None:
    out = resolve_ticket_outcome(_ticket(), TEAM, run_state="failed")
    assert out == {"state": "failed", "lane": "in-progress", "assignee": "eng-dev", "bounced": False, "advanced": False}


def test_an_approval_park_is_left_alone() -> None:
    out = resolve_ticket_outcome(_ticket(), TEAM, run_state="needs_approval")
    assert out["state"] == "needs_approval"
    assert out["bounced"] is False


def test_an_unassigned_ticket_is_not_a_handoff_and_bounces() -> None:
    # None != ran_as would read as "handed on mid-run" and leave it silently
    # unassigned; an unset assignee is never a handoff, so it must bounce.
    ticket = _ticket(assignee=None)
    out = resolve_ticket_outcome(ticket, TEAM, run_state="completed", ran_as="eng-dev")
    assert out == {"state": "pending", "lane": "backlog", "assignee": "eng-lead", "bounced": True, "advanced": False}


def test_a_reassigned_ticket_is_not_a_bounce() -> None:
    # The agent handed it on during the run; the ticket already moved.
    ticket = _ticket(assignee="eng-test", lane="testing")
    out = resolve_ticket_outcome(ticket, TEAM, run_state="completed", ran_as="eng-dev")
    assert out == {"state": "pending", "lane": "testing", "assignee": "eng-test", "bounced": False, "advanced": False}


def test_the_bounce_guard_blocks_after_three() -> None:
    ticket = _ticket(metadata={"bounces": 3})
    out = resolve_ticket_outcome(ticket, TEAM, run_state="completed")
    assert out["state"] == "blocked"


def test_a_team_without_a_bounce_lane_blocks_instead() -> None:
    team = TeamConfig.from_dict({"id": "x", "name": "X", "agents": [],
                                 "lanes": [{"id": "a"}, {"id": "done"}]})
    out = resolve_ticket_outcome(_ticket(lane="a"), team, run_state="completed")
    assert out["state"] == "blocked"


def test_qa_is_never_advanced_because_passing_and_rejecting_are_a_judgment() -> None:
    """test has two outgoing transitions — pass to the lead, or send it back to
    the dev. "The run ended" is not evidence for either, so it still bounces."""
    team = TeamConfig.from_dict({
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"},
                   {"id": "eng-test", "role": "test"}],
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
                  {"id": "ready-for-pr"}, {"id": "done"}],
    })
    out = resolve_ticket_outcome(_ticket(assignee="eng-test", lane="testing"),
                                 team, run_state="completed", ran_as="eng-test")
    assert out["advanced"] is False
    assert out["bounced"] is True
    assert out["assignee"] == "eng-lead"


def test_a_role_with_no_outgoing_rule_bounces() -> None:
    # devops has no transition of its own; nothing to advance to.
    team = TeamConfig.from_dict({
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-ops", "role": "devops"}],
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
                  {"id": "ready-for-pr"}, {"id": "done"}],
    })
    out = resolve_ticket_outcome(_ticket(assignee="eng-ops", lane="in-progress"),
                                 team, run_state="completed", ran_as="eng-ops")
    assert out["advanced"] is False
    assert out["bounced"] is True


def test_no_agent_fills_the_destination_role_so_nothing_is_advanced() -> None:
    """TEAM has a dev but no test agent. There is nowhere for the work to go,
    so the runtime must not invent an assignee."""
    out = resolve_ticket_outcome(_ticket(assignee="eng-dev", lane="in-progress"),
                                 TEAM, run_state="completed", ran_as="eng-dev")
    assert out["advanced"] is False
    assert out["bounced"] is True

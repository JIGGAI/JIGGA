"""What a finished run means for the ticket it worked.

Finishing a run is not finishing the work. Before this, the end of a successful
run wrote `completed` onto the task, so a ticket the dev had merely handed to QA
read as done — and on 2026-08-25 two tickets read `completed` having produced
nothing at all. A ticket is complete when it reaches the `done` lane and at no
other moment.

Pure function: it decides, the caller writes. That keeps the rule testable
without standing up an agent run.
"""
from __future__ import annotations

from typing import TypedDict

from jigga.core.models import Task, TeamConfig
from jigga.runtime.lanes import (
    DEFAULT_CLOSE_LANE,
    DONE_LANE,
    close_lane,
    lane_transitions,
)

# How many times a ticket may return to the lead unhandled before it stops.
# Bouncing is how unowned work finds an owner, but a lead that reassigns
# blindly would ping-pong forever; this bounds it loudly instead.
MAX_BOUNCES = 3


class TicketOutcome(TypedDict):
    state: str
    lane: str | None
    assignee: str | None
    bounced: bool


def _lead_of(team: TeamConfig) -> str | None:
    for member in team.agents or []:
        if isinstance(member, dict) and member.get("role") == "lead":
            return str(member.get("id")) if member.get("id") else None
    return None


def resolve_ticket_outcome(
    task: Task, team: TeamConfig, *, run_state: str, ran_as: str | None = None,
) -> TicketOutcome:
    """Decide the ticket's assignee/lane/state after a run.

    `ran_as` is the agent whose run just ended. When the ticket's assignee is
    someone else, the agent handed it on mid-run and there is nothing to bounce.
    """
    keep: TicketOutcome = {"state": run_state, "lane": task.lane,
                           "assignee": task.assignee, "bounced": False}

    # A failed or parked run leaves the board untouched — the work is still
    # where it was, and the reason is on the run record.
    if run_state != "completed":
        return keep

    if task.lane == DONE_LANE:
        return {**keep, "state": "completed"}

    # The run parked this ticket itself. `tickets.decompose` sets the epic to
    # `waiting` mid-run, and the run that did it then reaches this function
    # holding that same ticket — so without this the outcome resolution
    # overwrites `waiting` and bounces the epic it just parked. Observed live:
    # a lead decomposed a ticket into three stories correctly and the board
    # still showed the epic back in `backlog` with `bounces: 1`.
    #
    # A waiting ticket is deliberately parked, not stalled: it is waiting on its
    # children, and `release_parent_if_ready` is what wakes it. Nothing about
    # the run that parked it should move it.
    # `keep` carries the RUN's state, which is `completed` here — returning it
    # unchanged would complete the epic. Keep the ticket's own state instead.
    if task.state == "waiting":
        return {**keep, "state": "waiting"}

    # Handed on during the run: the ticket already moved, so re-queue it for
    # whoever holds it now.
    if ran_as is not None and task.assignee is not None and task.assignee != ran_as:
        return {**keep, "state": "pending"}

    # A ticket that has passed QA waits in the close lane for the lead to
    # confirm the merge, and waiting is not failing. Bouncing it would send
    # finished work back to `backlog` on the lead's first run without a merge,
    # and three such runs would block it outright — the exact opposite of the
    # "tickets sit in ready-for-pr until the lead closes them" the design
    # promises.
    if task.lane and task.lane == (close_lane(team) or DEFAULT_CLOSE_LANE):
        return {**keep, "state": "pending"}

    # Bounce it to the lead so it lands somewhere visible rather than sitting
    # silently assigned to an agent that is finished with it.
    bounces = int((task.metadata or {}).get("bounces") or 0)
    bounce_lane = lane_transitions(team)["bounce_lane"]
    lead = _lead_of(team)
    if bounces >= MAX_BOUNCES or not bounce_lane or not lead:
        return {**keep, "state": "blocked"}
    return {"state": "pending", "lane": bounce_lane, "assignee": lead, "bounced": True}

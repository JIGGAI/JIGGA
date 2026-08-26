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
    next_hop,
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
    # The runtime made the handoff the agent did not. Recorded so the move is
    # auditable as something the system did, not something the agent claimed.
    advanced: bool


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
                           "assignee": task.assignee, "bounced": False, "advanced": False}

    # A failed or parked run leaves the board untouched — the work is still
    # where it was, and the reason is on the run record.
    if run_state != "completed":
        return keep

    if task.lane == DONE_LANE:
        return {**keep, "state": "completed"}

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

    # Nobody has it next. Before bouncing, try to make the move the agent should
    # have made. A push pipeline only advances when the holder hands the ticket
    # on, and agents kept not doing it — the work stopped and the ticket bounced
    # until it blocked. Where the transition table leaves no choice, doing
    # nothing should carry the board forward rather than backward.
    #
    # A wrong advance is cheap and self-correcting: work that was not really
    # finished lands in QA and comes straight back, which is what the testing
    # lane is for. A bounce costs a blocked ticket and a person.
    # Only for the ordinary case: the agent held this ticket and finished. An
    # unassigned ticket is an anomaly, not a completed step — pushing it
    # downstream would hide that, so those still surface to the lead.
    if ran_as is not None and task.assignee == ran_as:
        hop = next_hop(team, ran_as)
        if hop is not None:
            assignee, lane = hop
            return {"state": "pending", "lane": lane, "assignee": assignee,
                    "bounced": False, "advanced": True}

    # Bounce it to the lead so it lands somewhere visible rather than sitting
    # silently assigned to an agent that is finished with it.
    bounces = int((task.metadata or {}).get("bounces") or 0)
    bounce_lane = lane_transitions(team)["bounce_lane"]
    lead = _lead_of(team)
    if bounces >= MAX_BOUNCES or not bounce_lane or not lead:
        return {**keep, "state": "blocked"}
    return {"state": "pending", "lane": bounce_lane, "assignee": lead,
            "bounced": True, "advanced": False}

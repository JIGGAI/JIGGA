"""The guard against two agents handing the same ticket back and forth forever.

`tickets.handoff` resets `bounces` to 0 on every successful handoff, and that
reset is correct: `bounces` counts a ticket returning to the lead *unowned*, and
leaving it unreset made it a lifetime budget that permanently blocked tickets
which had long since found an owner.

The cost of that reset is that a ticket which never goes unowned is never
counted at all. Two agents that each hand off cleanly, to each other, forever,
look perfectly healthy: every leg zeroes the counter the previous leg set.

Observed live: dev finished a story it could not test, handed it to QA saying
"QA should run `npm test`"; QA could not run it either, failed it back asking dev
for a verified result; repeat. Six round trips in eleven minutes, `bounces: 0`
throughout, board showing an ordinary in-progress ticket the whole time. It would
not have stopped on its own.

So loops are counted per *pair* of agents, separately from `bounces`, and they
ladder rather than latch:

1. Under the limit — nothing happens.
2. At the limit — the handoff is redirected to the team lead, once, with the
   reason attached. Two agents that cannot agree get a third who can decide, and
   the loop keeps moving rather than pausing for a human.
3. The same pair loops again after the lead already ruled — the ticket is
   blocked. The lead's intervention did not take, and something outside the
   board is wrong. Blocked is visible; another lap is not.

Counts are kept as a `{pair: n}` map rather than a handoff log: bounded in size,
and the audit trail already holds the full history for anyone reading it back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Handoffs, not round trips: a round trip is two. Six lets a genuine
# fix-review-fix-review exchange finish, and stops the third fruitless lap.
MAX_PAIR_HANDOFFS = 6


def pair_key(one: str, other: str) -> str:
    """A stable name for an unordered pair. dev→test and test→dev are the same
    loop seen from two sides, and counting them separately would double the
    budget of the exact thing being counted."""
    return "|".join(sorted([str(one), str(other)]))


@dataclass(frozen=True)
class LoopVerdict:
    """What to do with a handoff. `redirect_to` and `block` are never both set."""

    redirect_to: str | None = None
    block: bool = False
    reason: str | None = None

    @property
    def intervened(self) -> bool:
        return self.block or self.redirect_to is not None


def evaluate_handoff_loop(
    counts: dict[str, Any] | None,
    escalated: list[str] | None,
    actor: str,
    assignee: str,
    *,
    lead: str | None,
    limit: int = MAX_PAIR_HANDOFFS,
) -> LoopVerdict:
    """Whether this handoff continues a loop, given what the ticket has seen.

    `counts` maps pair_key to handoffs already made between that pair; this
    handoff is not in it yet. `escalated` names pairs the lead has already
    ruled on.
    """
    if not actor or not assignee or actor == assignee:
        return LoopVerdict()

    key = pair_key(actor, assignee)
    seen = int((counts or {}).get(key) or 0)
    if seen + 1 < max(1, int(limit)):
        return LoopVerdict()

    already_ruled = key in list(escalated or [])
    reason = (
        f"{actor} and {assignee} have handed this ticket back and forth "
        f"{seen + 1} times without finishing it."
    )

    # The lead can only break a loop it is not already half of. A lead-and-dev
    # loop redirected to the lead is the same two agents and the same lap.
    if not already_ruled and lead and lead not in (actor, assignee):
        return LoopVerdict(redirect_to=lead, reason=reason + " Sending it to the lead to decide.")

    if already_ruled:
        return LoopVerdict(block=True, reason=reason + " The lead already ruled on this pair once.")
    return LoopVerdict(block=True, reason=reason + " No lead outside the pair can break the tie.")

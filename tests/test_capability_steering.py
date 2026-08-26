"""The tool descriptions must route an agent to the right verb.

A lead holding a ticket used `task.assign` to pass it on, twice in a row,
because that is the familiar tool and nothing in what the model was shown said
not to. The result was a duplicate ticket on the board and the original
bouncing until it blocked. `summary` and `when_to_use` ARE the routing signal —
the model picks its tool from those lines alone — so they are worth asserting.
"""
from __future__ import annotations

from jigga.runtime.agent import _parameters_for
from jigga.runtime.capabilities import bundled_capabilities


def _cap(action: str):
    return next(c for c in bundled_capabilities() if action in c.actions)


def _routing_text(action: str) -> str:
    cap = _cap(action)
    return f"{cap.summary} {cap.when_to_use or ''}".lower()


def test_task_assign_says_it_is_for_new_work_only() -> None:
    text = _routing_text("task.assign")
    assert "new" in text
    assert "tickets.handoff" in text, "must name the alternative, not just forbid itself"


def test_the_tickets_capability_names_handoff_as_the_way_work_moves() -> None:
    text = _routing_text("tickets.handoff")
    assert "tickets.handoff" in text
    assert "already exists" in text or "already exist" in text


def test_both_sides_warn_against_using_task_assign_for_a_handoff() -> None:
    # Whichever tool the model is looking at, it should learn the same rule.
    assert "task.assign" in _routing_text("tickets.handoff")
    assert "tickets.handoff" in _routing_text("task.assign")


def test_the_task_assign_argument_list_repeats_it() -> None:
    # The description the model reads when it is already filling in arguments.
    title = _parameters_for("task.assign", _cap("task.assign"))["properties"]["title"]
    assert "tickets.handoff" in title["description"]

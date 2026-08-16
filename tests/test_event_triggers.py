"""Event triggers (#151) — fire because something is true, not because the
clock said so.

Only `trigger.schedule` fired before this. A schedule answers "every weekday at
09:00"; it can't answer "15 minutes before any meeting", because the thing
being waited on is a *subject* whose time you have to look up.

The two properties that make this correct rather than merely working:

**Per-subject dedup.** "15m before any meeting" is true on every tick inside
that window. Cron's time-bucketed dedup would let a 60-second heartbeat fire it
fifteen times for one meeting. The key is (workflow, trigger, subject).

**Explicit credentials.** Evaluating the trigger means reading somebody's
calendar. The trigger has to name whose. Inferring it — "the first node's
agent" — would make credential use implicit, and there is no safe default.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.config import load_workflows
from jigga.core.io import write_yaml
from jigga.runtime import triggers
from jigga.runtime.triggers import TriggerError, due_event_triggers, parse_offset


def _workflow(paths, workflow_id: str, trigger: dict) -> None:
    write_yaml(paths.workflows / f"{workflow_id}.yaml", {
        "id": workflow_id, "name": workflow_id, "trigger": trigger,
        "steps": [{"id": "s1", "agent": "prep", "action": "summarize"}]})


def _agent(paths, agent_id: str = "prep") -> None:
    write_yaml(paths.agents / f"{agent_id}.yaml",
               {"id": agent_id, "name": agent_id, "role": "r", "tools": []})


def _fake_calendar(monkeypatch, events: list[dict]) -> None:
    """Stand in for the calendar capability so the trigger logic is what's under
    test, not the connector."""
    monkeypatch.setitem(
        triggers.EVENT_EVALUATORS, "calendar_event_upcoming",
        lambda paths, workflow, trigger, now: [
            {"id": e["id"], "payload": e}
            for e in events
            if now <= datetime.fromisoformat(e["start"]) <= now + parse_offset(trigger.get("offset"))
        ])


NOW = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)


def _at(minutes: int) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat()


# --- offsets -------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("15m", timedelta(minutes=15)), ("2h", timedelta(hours=2)), ("1d", timedelta(days=1)),
    ("30 minutes", timedelta(minutes=30)), (45, timedelta(minutes=45)), (None, timedelta(0)),
])
def test_offsets_parse(raw, expected) -> None:
    assert parse_offset(raw) == expected


def test_an_unparseable_offset_is_rejected_loudly() -> None:
    """Silently treating 'soon' as zero would make the trigger never fire, with
    nothing to explain why."""
    with pytest.raises(TriggerError, match="unparseable"):
        parse_offset("soon")


# --- firing ---------------------------------------------------------------------


def test_a_subject_inside_the_window_fires(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    _agent(paths)
    _workflow(paths, "meeting_prep",
              {"event": "calendar_event_upcoming", "offset": "15m", "agent": "prep"})
    _fake_calendar(monkeypatch, [{"id": "evt1", "title": "Standup", "start": _at(10)}])

    events = due_event_triggers(paths, load_workflows(paths.workflows), {}, NOW)

    assert [e.type for e in events] == ["workflow.event_due"]
    assert events[0].payload["workflow"] == "meeting_prep"
    assert events[0].payload["subject"] == "evt1"
    assert events[0].payload["trigger_payload"]["title"] == "Standup"


def test_a_subject_outside_the_window_does_not_fire(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    _agent(paths)
    _workflow(paths, "meeting_prep",
              {"event": "calendar_event_upcoming", "offset": "15m", "agent": "prep"})
    _fake_calendar(monkeypatch, [{"id": "evt1", "title": "Later", "start": _at(120)}])

    assert due_event_triggers(paths, load_workflows(paths.workflows), {}, NOW) == []


def test_a_scheduled_workflow_is_untouched(tmp_path: Path) -> None:
    """Event triggers must not disturb the existing schedule path."""
    paths = init_runtime(tmp_path)
    _workflow(paths, "daily", {"schedule": "weekdays at 09:00"})
    assert due_event_triggers(paths, load_workflows(paths.workflows), {}, NOW) == []


# --- the dedup that matters -----------------------------------------------------


def test_one_subject_fires_once_across_many_ticks(tmp_path: Path, monkeypatch) -> None:
    """The bug this prevents: the trigger is true for the whole 15-minute
    window, so a 60-second heartbeat would start fifteen runs for one meeting."""
    paths = init_runtime(tmp_path)
    _agent(paths)
    _workflow(paths, "meeting_prep",
              {"event": "calendar_event_upcoming", "offset": "15m", "agent": "prep"})
    _fake_calendar(monkeypatch, [{"id": "evt1", "title": "Standup", "start": _at(14)}])
    workflows = load_workflows(paths.workflows)
    state: dict = {}

    fired = 0
    for minute in range(15):          # one tick a minute across the whole window
        fired += len(due_event_triggers(paths, workflows, state, NOW + timedelta(minutes=minute)))

    assert fired == 1, f"fired {fired} times for one meeting"


def test_a_different_subject_still_fires(tmp_path: Path, monkeypatch) -> None:
    """Dedup must be per-subject, not per-workflow — two meetings in the same
    window are two runs."""
    paths = init_runtime(tmp_path)
    _agent(paths)
    _workflow(paths, "meeting_prep",
              {"event": "calendar_event_upcoming", "offset": "15m", "agent": "prep"})
    _fake_calendar(monkeypatch, [
        {"id": "evt1", "title": "Standup", "start": _at(5)},
        {"id": "evt2", "title": "Review", "start": _at(10)}])
    state: dict = {}

    events = due_event_triggers(paths, load_workflows(paths.workflows), state, NOW)

    assert sorted(e.payload["subject"] for e in events) == ["evt1", "evt2"]


def test_fired_keys_are_pruned_so_loop_state_stays_bounded(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    _agent(paths)
    _workflow(paths, "meeting_prep",
              {"event": "calendar_event_upcoming", "offset": "15m", "agent": "prep"})
    _fake_calendar(monkeypatch, [])
    state = {"event_fired": {
        "old|calendar_event_upcoming|ancient": (NOW - timedelta(days=30)).isoformat(),
        "new|calendar_event_upcoming|recent": (NOW - timedelta(hours=1)).isoformat()}}

    due_event_triggers(paths, load_workflows(paths.workflows), state, NOW)

    assert list(state["event_fired"]) == ["new|calendar_event_upcoming|recent"]


# --- misconfiguration is loud ---------------------------------------------------


def test_a_trigger_without_an_agent_is_an_error_not_a_silent_skip(tmp_path: Path) -> None:
    """Evaluating spends someone's credentials. There is no safe default, so
    the trigger must say whose — and saying nothing must be reported."""
    paths = init_runtime(tmp_path)
    _workflow(paths, "meeting_prep", {"event": "calendar_event_upcoming", "offset": "15m"})

    events = due_event_triggers(paths, load_workflows(paths.workflows), {}, NOW)

    assert [e.type for e in events] == ["workflow.trigger_error"]
    assert "must name the `agent:`" in events[0].payload["error"]


def test_an_unknown_event_name_is_reported(tmp_path: Path) -> None:
    """"Nothing matched" and "this trigger doesn't exist" must not look alike."""
    paths = init_runtime(tmp_path)
    _workflow(paths, "wat", {"event": "when_pigs_fly", "agent": "prep"})

    events = due_event_triggers(paths, load_workflows(paths.workflows), {}, NOW)

    assert [e.type for e in events] == ["workflow.trigger_error"]
    assert "unknown event trigger" in events[0].payload["error"]


def test_one_broken_trigger_does_not_stop_the_others(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    _agent(paths)
    _workflow(paths, "broken", {"event": "when_pigs_fly", "agent": "prep"})
    _workflow(paths, "working",
              {"event": "calendar_event_upcoming", "offset": "15m", "agent": "prep"})
    _fake_calendar(monkeypatch, [{"id": "evt1", "title": "Standup", "start": _at(5)}])

    events = due_event_triggers(paths, load_workflows(paths.workflows), {}, NOW)

    kinds = {e.type for e in events}
    assert kinds == {"workflow.trigger_error", "workflow.event_due"}


def test_an_evaluator_that_raises_is_contained(tmp_path: Path, monkeypatch) -> None:
    """A calendar outage must degrade to a reported error, not a broken tick."""
    paths = init_runtime(tmp_path)
    _agent(paths)
    _workflow(paths, "meeting_prep",
              {"event": "calendar_event_upcoming", "offset": "15m", "agent": "prep"})
    monkeypatch.setitem(triggers.EVENT_EVALUATORS, "calendar_event_upcoming",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("calendar down")))

    events = due_event_triggers(paths, load_workflows(paths.workflows), {}, NOW)

    assert events[0].type == "workflow.trigger_error"
    assert "calendar down" in events[0].payload["error"]


# --- the payload reaches the run ------------------------------------------------


def test_the_firing_event_is_referenceable_by_a_step(tmp_path: Path) -> None:
    """`${trigger.title}` should address the firing event the same way a step
    references any other output — not through a second templating dialect."""
    from jigga.runtime.dispatcher import resolve_value, seed_trigger_outputs

    outputs: dict = {}
    seed_trigger_outputs(outputs, {"id": "evt1", "title": "Standup", "attendees": ["a", "b"]})

    assert resolve_value("${trigger.title}", outputs) == "Standup"
    assert resolve_value("${trigger}", outputs)["id"] == "evt1"


def test_a_typo_in_a_trigger_reference_fails_closed(tmp_path: Path) -> None:
    """For push triggers this payload is externally controlled, so a silently
    literal `${trigger.tilte}` is the exact shape that published 20 unapproved
    items on the precursor stack."""
    from jigga.runtime.dispatcher import UnresolvedReferenceError, resolve_value, seed_trigger_outputs

    outputs: dict = {}
    seed_trigger_outputs(outputs, {"title": "Standup"})
    with pytest.raises(UnresolvedReferenceError):
        resolve_value("${trigger.tilte}", outputs)


def test_no_trigger_seeds_nothing(tmp_path: Path) -> None:
    """Scheduled and manual runs must not acquire a phantom `trigger` output."""
    from jigga.runtime.dispatcher import seed_trigger_outputs

    outputs: dict = {}
    seed_trigger_outputs(outputs, None)
    assert outputs == {}


# --- the shapes providers actually emit -----------------------------------------


def test_the_real_evaluator_fires_against_the_bundled_calendar(tmp_path: Path) -> None:
    """Not a stub. My first implementation read `output["events"]` and an ISO
    `start` key; the bundled capability returns a bare LIST whose items carry a
    wall-clock `time`. It silently never fired — the trigger looked fine and did
    nothing, which is the exact failure mode this codebase keeps closing.
    """
    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "prep.yaml", {
        "id": "prep", "name": "prep", "role": "r",
        "tools": ["calendar.list_events"], "permissions": {"calendar": "read"}})
    _workflow(paths, "meeting_prep",
              {"event": "calendar_event_upcoming", "offset": "24h", "agent": "prep"})

    at_nine = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
    events = due_event_triggers(paths, load_workflows(paths.workflows), {}, at_nine)

    assert [e.type for e in events] == ["workflow.event_due", "workflow.event_due"]
    assert {e.payload["trigger_payload"]["title"] for e in events} == {"Planning block", "Project review"}


@pytest.mark.parametrize("raw,expected_hour", [
    ("2026-08-16T14:30:00+00:00", 14),   # Google, timed event
    ("14:30", 14),                        # bundled dry-run, wall clock
])
def test_both_provider_time_formats_parse(raw, expected_hour) -> None:
    from jigga.runtime.triggers import _as_datetime

    reference = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
    parsed = _as_datetime(raw, relative_to=reference)
    assert parsed is not None and parsed.hour == expected_hour


def test_an_all_day_date_parses_rather_than_being_dropped() -> None:
    """Google emits a bare date for all-day events."""
    from jigga.runtime.triggers import _as_datetime

    assert _as_datetime("2026-08-16", relative_to=None) is not None


def test_a_dict_shaped_response_is_also_accepted(tmp_path: Path, monkeypatch) -> None:
    """The Google connector returns {events: [...]}; the trigger must not depend
    on which provider happens to be installed."""
    from jigga.runtime import triggers as triggers_module

    paths = init_runtime(tmp_path)
    _agent(paths)
    _workflow(paths, "meeting_prep",
              {"event": "calendar_event_upcoming", "offset": "1h", "agent": "prep"})
    monkeypatch.setattr(triggers_module, "_calendar_event_upcoming",
                        triggers_module.EVENT_EVALUATORS["calendar_event_upcoming"])
    monkeypatch.setattr("jigga.runtime.dispatcher.dispatch_action",
                        lambda *_a, **_k: {"events": [{"id": "g1", "title": "Sync", "time": _at(20)}]})

    events = due_event_triggers(paths, load_workflows(paths.workflows), {}, NOW)

    assert [e.payload["subject"] for e in events] == ["g1"]


def test_a_subject_id_is_stable_when_the_provider_gives_none(tmp_path: Path, monkeypatch) -> None:
    """Dedup depends on the fallback id being repeatable across ticks — if it
    varied, every tick would look like a new meeting."""

    paths = init_runtime(tmp_path)
    _agent(paths)
    _workflow(paths, "meeting_prep",
              {"event": "calendar_event_upcoming", "offset": "1h", "agent": "prep"})
    monkeypatch.setattr("jigga.runtime.dispatcher.dispatch_action",
                        lambda *_a, **_k: [{"title": "No Id Here", "time": _at(20)}])
    workflows = load_workflows(paths.workflows)

    first = due_event_triggers(paths, workflows, {}, NOW)
    second = due_event_triggers(paths, workflows, {}, NOW + timedelta(minutes=1))

    assert first[0].payload["subject"] == second[0].payload["subject"]

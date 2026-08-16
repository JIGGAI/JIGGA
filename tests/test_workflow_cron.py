"""Workflows can use cron, not only the friendly forms.

Agents already accepted full 5-field cron via `wake.schedules[].cron`. Workflow
`trigger.schedule` went through a separate friendly-string parser that
understands "weekdays at 09:00" and nothing else — so a monthly schedule could
not be expressed at all. That is not a hypothetical gap: the HMX marketing
calendar runs on *day 20 of the month at 10:00*, which had no representation.

The risky part of closing it is detection. `"every day at 6:30 pm"` is also
five whitespace-separated tokens, so a bare field count would misread a working
friendly schedule as cron and silently stop it firing — trading one missing
feature for a regression in the feature that worked. Detection therefore checks
the *shape* of every field, not just how many there are.

While here: a schedule that parses as neither form used to fire never and
explain nothing. It is now a validation error.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.config import load_workflows
from jigga.core.io import write_yaml
from jigga.runtime.scheduler import (
    due_events,
    looks_like_cron,
    schedule_due,
    schedule_problem,
)


# --- detection: the part that could break what already worked ------------------


@pytest.mark.parametrize("text", [
    "0 10 20 * *",          # day 20 at 10:00 — the HMX monthly calendar
    "*/15 * * * *",
    "0 9 * * MON-FRI",
    "30 6 1,15 * *",
    "0 0 1 JAN *",
])
def test_cron_is_recognized(text) -> None:
    assert looks_like_cron(text) is True


@pytest.mark.parametrize("text", [
    "every day at 6:30 pm",     # five tokens, and NOT cron
    "weekdays at 09:00",
    "weekend 10am",
    "daily 9:00",
    "on the first monday of the month",
])
def test_friendly_forms_are_not_mistaken_for_cron(text) -> None:
    """The regression this guards: five tokens is not enough to mean cron."""
    assert looks_like_cron(text) is False


# --- firing ---------------------------------------------------------------------


def test_a_monthly_cron_workflow_fires_on_the_right_day(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    write_yaml(paths.workflows / "calendar.yaml", {
        "id": "calendar", "name": "calendar",
        "trigger": {"schedule": "0 10 20 * *"},
        "steps": [{"id": "s1", "agent": "a", "action": "summarize"}]})
    workflows = load_workflows(paths.workflows)

    on_the_day = due_events(paths.agents, paths.workflows,
                            datetime(2026, 9, 20, 10, 0), workflows=workflows, agents={})
    wrong_day = due_events(paths.agents, paths.workflows,
                           datetime(2026, 9, 19, 10, 0), workflows=workflows, agents={})
    wrong_hour = due_events(paths.agents, paths.workflows,
                            datetime(2026, 9, 20, 11, 0), workflows=workflows, agents={})

    assert [e.payload["workflow"] for e in on_the_day] == ["calendar"]
    assert wrong_day == [] and wrong_hour == []


def test_friendly_schedules_still_fire(tmp_path: Path) -> None:
    """The existing path must be untouched — this is an addition, not a swap."""
    paths = init_runtime(tmp_path)
    write_yaml(paths.workflows / "brief.yaml", {
        "id": "brief", "name": "brief",
        "trigger": {"schedule": "weekdays at 09:00"},
        "steps": [{"id": "s1", "agent": "a", "action": "summarize"}]})
    workflows = load_workflows(paths.workflows)

    weekday = due_events(paths.agents, paths.workflows,
                         datetime(2026, 8, 17, 9, 0), workflows=workflows, agents={})   # Monday
    weekend = due_events(paths.agents, paths.workflows,
                         datetime(2026, 8, 16, 9, 0), workflows=workflows, agents={})   # Sunday

    assert [e.payload["workflow"] for e in weekday] == ["brief"]
    assert weekend == []


@pytest.mark.parametrize("moment,due", [
    (datetime(2026, 8, 17, 9, 0), True),     # Monday
    (datetime(2026, 8, 21, 9, 0), True),     # Friday
    (datetime(2026, 8, 22, 9, 0), False),    # Saturday
])
def test_weekday_names_work_in_a_workflow_cron(moment, due) -> None:
    assert schedule_due("0 9 * * MON-FRI", moment) is due


def test_a_malformed_cron_never_fires_rather_than_raising() -> None:
    """The scheduler runs at the top of every tick — one bad schedule must not
    take the heartbeat down."""
    assert schedule_due("x * * * *", datetime(2026, 8, 17, 9, 0)) is False
    assert schedule_due("*/0 * * * *", datetime(2026, 8, 17, 9, 0)) is False


# --- schedules that will never fire are now loud --------------------------------


def test_a_valid_schedule_reports_no_problem() -> None:
    assert schedule_problem("0 10 20 * *") is None
    assert schedule_problem("weekdays at 09:00") is None


def test_an_unrecognized_schedule_is_reported() -> None:
    """It used to fire never and explain nothing."""
    problem = schedule_problem("whenever I feel like it")
    assert problem and "unrecognized schedule" in problem


def test_an_out_of_range_cron_field_is_reported() -> None:
    """`0 25 * * *` is well-formed and can never match an hour."""
    problem = schedule_problem("0 25 * * *")
    assert problem and "hour" in problem and "out of range" in problem


def test_an_empty_schedule_is_reported() -> None:
    assert schedule_problem("") == "schedule is empty"
    assert schedule_problem(None) == "schedule is empty"


def test_validate_surfaces_a_broken_workflow_schedule(tmp_path: Path) -> None:
    from jigga.runtime.validation import validate_configs

    paths = init_runtime(tmp_path)
    write_yaml(paths.workflows / "broken.yaml", {
        "id": "broken", "name": "broken",
        "trigger": {"schedule": "sometime next tuesday-ish"},
        "steps": [{"id": "s1", "agent": "a", "action": "summarize"}]})

    problems = validate_configs({}, {}, load_workflows(paths.workflows))

    assert any("broken" in p and "unrecognized schedule" in p for p in problems)


def test_validate_is_quiet_about_good_schedules(tmp_path: Path) -> None:
    from jigga.runtime.validation import validate_configs

    paths = init_runtime(tmp_path)
    for workflow_id, schedule in (("monthly", "0 10 20 * *"), ("daily", "weekdays at 09:00")):
        write_yaml(paths.workflows / f"{workflow_id}.yaml", {
            "id": workflow_id, "name": workflow_id, "trigger": {"schedule": schedule},
            "steps": [{"id": "s1", "agent": "a", "action": "summarize"}]})

    problems = validate_configs({}, {}, load_workflows(paths.workflows))

    assert [p for p in problems if "schedule" in p] == []


def test_a_workflow_without_a_schedule_is_not_flagged(tmp_path: Path) -> None:
    """Manual and event-triggered workflows have no schedule and must not be
    reported as broken."""
    from jigga.runtime.validation import validate_configs

    paths = init_runtime(tmp_path)
    write_yaml(paths.workflows / "manual.yaml", {
        "id": "manual", "name": "manual", "trigger": {"manual": True},
        "steps": [{"id": "s1", "agent": "a", "action": "summarize"}]})

    assert [p for p in validate_configs({}, {}, load_workflows(paths.workflows))
            if "schedule" in p] == []

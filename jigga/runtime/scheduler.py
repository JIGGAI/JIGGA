from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jigga.core.config import load_agents, load_workflows
from jigga.runtime.events import JiggaEvent

# Supported friendly-schedule patterns for workflow triggers. Cron strings on
# agents use the full 5-field parser below; workflow `trigger.schedule:` strings
# go through this generalized parser.
#
# Examples:
#   "weekday 7:30am"        → Mon–Fri at 07:30
#   "weekdays at 17:00"     → Mon–Fri at 17:00
#   "daily 9:00"            → every day at 09:00
#   "weekend 10am"          → Sat–Sun at 10:00
#   "every day at 6:30pm"   → every day at 18:30
_TIME_PATTERN = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)?|(\d{1,2})\s*(am|pm)", re.IGNORECASE)


def _cron_due(cron: str, now: datetime) -> bool:
    parts = cron.split()
    if len(parts) != 5:
        return False
    minute, hour, day_of_month, month, day_of_week = parts
    checks = [
        _field_matches(minute, now.minute),
        _field_matches(hour, now.hour),
        _field_matches(day_of_month, now.day),
        _field_matches(month, now.month),
        _weekday_matches(day_of_week, now.weekday()),
    ]
    return all(checks)


def _field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        return value % int(field[2:]) == 0
    if "," in field:
        return any(_field_matches(part, value) for part in field.split(","))
    if "-" in field:
        start, end = [int(part) for part in field.split("-", 1)]
        return start <= value <= end
    return int(field) == value


def _weekday_matches(field: str, weekday: int) -> bool:
    # Python uses Monday=0; cron commonly uses Sunday=0/7, Monday=1.
    cron_weekday = (weekday + 1) % 7
    names = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
    normalized = field.upper().replace("7", "0")
    for name, number in names.items():
        normalized = normalized.replace(name, str(number))
    return _field_matches(normalized, cron_weekday)


def _parse_friendly_time(text: str) -> tuple[int, int] | None:
    match = _TIME_PATTERN.search(text)
    if not match:
        return None
    if match.group(1) is not None:
        hour = int(match.group(1))
        minute = int(match.group(2))
        period = (match.group(3) or "").lower()
    else:
        hour = int(match.group(4))
        minute = 0
        period = (match.group(5) or "").lower()
    if period == "pm" and hour < 12:
        hour += 12
    elif period == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _friendly_schedule_due(schedule: str, now: datetime) -> bool:
    lowered = schedule.lower()
    if ("weekday" in lowered or "weekdays" in lowered) and now.weekday() >= 5:
        return False
    if "weekend" in lowered and now.weekday() < 5:
        return False
    parsed = _parse_friendly_time(lowered)
    if parsed is None:
        return False
    hour, minute = parsed
    return now.hour == hour and now.minute == minute


def due_events(agents_dir: Path, workflows_dir: Path, now: datetime | None = None) -> list[JiggaEvent]:
    current = now or datetime.now()
    events: list[JiggaEvent] = []

    for agent in load_agents(agents_dir).values():
        for schedule in agent.wake.get("schedules", []):
            cron = schedule.get("cron")
            if cron and _cron_due(cron, current):
                events.append(
                    JiggaEvent.create(
                        "cron.tick",
                        "scheduler",
                        targets=[agent.id],
                        schedule=schedule.get("event", cron),
                        cron=cron,
                        message=schedule.get("message"),
                    )
                )

    for workflow in load_workflows(workflows_dir).values():
        schedule = workflow.trigger.get("schedule")
        if isinstance(schedule, str) and _friendly_schedule_due(schedule, current):
            events.append(
                JiggaEvent.create(
                    "workflow.schedule_due",
                    "scheduler",
                    targets=[workflow.id],
                    workflow=workflow.id,
                    schedule=schedule,
                )
            )
    return events


def serialize_events(events: list[JiggaEvent]) -> list[dict[str, Any]]:
    return [event.to_dict() for event in events]

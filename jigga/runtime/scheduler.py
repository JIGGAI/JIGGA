from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from jigga.core.config import load_agents, load_workflows
from jigga.runtime.events import JiggaEvent


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


def _friendly_schedule_due(schedule: str, now: datetime) -> bool:
    lowered = schedule.lower()
    if "weekday" in lowered and now.weekday() >= 5:
        return False
    if "7:30" in lowered or "07:30" in lowered:
        return now.hour == 7 and now.minute == 30
    return False


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

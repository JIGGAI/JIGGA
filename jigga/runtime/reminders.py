"""One-shot reminders — `remind.at` / `remind.list`, fired by the supervisor.

Cron covers *recurring* wakes; "remind me at 5pm" had no primitive — an agent
had to abuse a cron schedule that then fired forever. A reminder is a file
under `~/.jigga/reminders/<id>.json` (file-first, auditable) with a due time;
the supervisor sweep fires each one exactly once: it creates a task for the
target agent (default agent unless the reminder names one), which rides the
normal wake pipeline — throttles, context pack, channel delivery via the
agent's `notifications.send`.

Due-time input: ISO-8601 (`2026-07-28T17:00:00+00:00`) or a relative `in`
offset (`30m`, `2h`, `1d`, `90s`). Naive ISO times are taken as UTC.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, read_json, write_json
from jigga.core.models import now_iso
from jigga.runtime.audit import append_event, new_id

_OFFSET = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.I)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
# One sweep fires at most this many reminders — bounds tick work like every
# other supervisor job.
MAX_FIRED_PER_SWEEP = 20


def _dir(home: Path) -> Path:
    return Path(home) / "reminders"


def parse_due(at: str | None, offset: str | None, *, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if at:
        parsed = datetime.fromisoformat(str(at))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    match = _OFFSET.match(str(offset or ""))
    if not match:
        raise ValueError("remind.at requires input.at (ISO datetime) or input.in (e.g. 30m, 2h, 1d)")
    return now + timedelta(seconds=int(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()])


def create_reminder(home: Path, *, message: str, at: str | None = None, offset: str | None = None,
                    agent: str | None = None, created_by: str | None = None,
                    now: datetime | None = None) -> dict[str, Any]:
    if not message or not str(message).strip():
        raise ValueError("remind.at requires input.message")
    due = parse_due(at, offset, now=now)
    record = {
        "id": new_id("reminder"), "message": str(message).strip(),
        "due_at": due.isoformat(), "agent": agent, "created_by": created_by,
        "created_at": now_iso(), "status": "pending", "fired_at": None,
    }
    ensure_dir(_dir(home))
    write_json(_dir(home) / f"{record['id']}.json", record)
    return record


def list_reminders(home: Path, *, include_fired: bool = False) -> list[dict[str, Any]]:
    records = []
    directory = _dir(home)
    for path in sorted(directory.glob("*.json")) if directory.exists() else []:
        try:
            record = read_json(path)
        except (OSError, ValueError):
            continue
        if include_fired or record.get("status") == "pending":
            records.append(record)
    return sorted(records, key=lambda r: r.get("due_at", ""))


def fire_due_reminders(home: Path, logs_dir: Path, tasks_dir: Path, agents_dir: Path,
                       *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Supervisor sweep: fire every pending reminder whose due time has passed
    (bounded per sweep). Firing = create a task for the target agent + mark the
    reminder fired (before task creation would lose the reminder on a crash;
    after means a crash between the two re-fires — duplicate task, never a
    lost reminder — the right failure mode)."""
    from jigga.core.config import load_agents, resolve_default_agent

    now = now or datetime.now(timezone.utc)
    fired: list[dict[str, Any]] = []
    due = [r for r in list_reminders(home)
           if datetime.fromisoformat(r["due_at"]) <= now][:MAX_FIRED_PER_SWEEP]
    if not due:
        return fired
    agents = load_agents(agents_dir)
    default_agent = resolve_default_agent(agents_dir)
    for record in due:
        target = record.get("agent") or default_agent
        if target not in agents:
            target = default_agent
        if target is None:  # no default agent configured — leave pending, audit once per sweep
            append_event(logs_dir, "reminder.unroutable", status="error", reminder=record["id"])
            continue
        from jigga.runtime.tasks import create_task

        task = create_task(
            tasks_dir,
            title=f"Reminder: {record['message'][:60]}",
            description=(f"One-shot reminder set {record['created_at']}"
                         + (f" by {record['created_by']}" if record.get("created_by") else "")
                         + f", due {record['due_at']}:\n\n{record['message']}\n\n"
                         "Deliver this to the user (notifications.send) or act on it as appropriate."),
            assignee=target,
            metadata={"reminder": record["id"]},
        )
        record["status"] = "fired"
        record["fired_at"] = now_iso()
        record["task_id"] = task.id
        write_json(_dir(home) / f"{record['id']}.json", record)
        append_event(logs_dir, "reminder.fired", reminder=record["id"], agent=target, task_id=task.id)
        fired.append(record)
    return fired


def reminders_handler(step, _capability, resolved_input, _memory_context, runtime) -> Any:
    data = resolved_input if isinstance(resolved_input, dict) else {}
    if step.action == "remind.at":
        record = create_reminder(
            runtime.home,
            message=str(data.get("message") or ""),
            at=data.get("at"), offset=data.get("in"),
            agent=data.get("agent") or (runtime.agent.id if runtime.agent else None),
            created_by=runtime.agent.id if runtime.agent else "cli",
        )
        return {"source": "capability.reminders", **record}
    if step.action == "remind.list":
        return {"source": "capability.reminders",
                "reminders": list_reminders(runtime.home, include_fired=bool(data.get("include_fired")))}
    raise ValueError(f"Unknown reminder action: {step.action}")

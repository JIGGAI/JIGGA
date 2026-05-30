"""Audit-log rotation & retention (Milestone C).

`~/.jigga/logs/events.jsonl` is append-only and otherwise grows forever. This
rolls the active log over — by calendar day, or when it crosses a size cap —
into dated archives (`events-YYYY-MM-DD.jsonl`) and prunes archives past a
retention window.

Rotation runs on the supervisor heartbeat (`supervisor_tick`), not on every
`append_event`, so the write path stays free of config loads and file stats.
The work is cheap: a `stat` plus, at a day boundary, one `rename` and a prune.

Readers (`audit_query.read_events`) transparently fold the archives back in, so
`jigga audit` / `trace` / `cost` and the budget windows still see history across
a rollover — they're not limited to "today".

Config (all optional; sensible defaults):

    logs:
      rotation:
        enabled: true
        max_bytes: 10485760     # 10 MiB — size-triggered rollover
        retention_days: 30      # delete dated archives older than this
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jigga.core.config import load_runtime_config

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 30

ACTIVE_LOG = "events.jsonl"
# events-2026-05-30.jsonl  or  events-2026-05-30.2.jsonl (same-day size splits)
_ARCHIVE_RE = re.compile(r"^events-(\d{4}-\d{2}-\d{2})(?:\.\d+)?\.jsonl$")


def _rotation_config(home: Path) -> dict[str, Any]:
    return dict((load_runtime_config(home).get("logs") or {}).get("rotation") or {})


def _first_event_date(path: Path) -> date | None:
    """The calendar day the active log started, from its first line's `time`.
    Reads only one line — O(1), not the whole file."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            line = handle.readline().strip()
    except OSError:
        return None
    if not line:
        return None
    try:
        stamp = json.loads(line).get("time", "")
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed.date()


def _archive_path(logs_dir: Path, day: date) -> Path:
    base = logs_dir / f"events-{day.isoformat()}.jsonl"
    if not base.exists():
        return base
    # Same-day size-triggered split — disambiguate with a counter.
    counter = 1
    while (candidate := logs_dir / f"events-{day.isoformat()}.{counter}.jsonl").exists():
        counter += 1
    return candidate


def _archive_date(name: str) -> date | None:
    match = _ARCHIVE_RE.match(name)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def archive_files(logs_dir: Path) -> list[Path]:
    """Dated archives, oldest first."""
    archives = [p for p in logs_dir.glob("events-*.jsonl") if _archive_date(p.name) is not None]
    return sorted(archives, key=lambda p: p.name)


def _prune(logs_dir: Path, now: datetime, retention_days: int) -> list[str]:
    if retention_days <= 0:
        return []
    cutoff = now.date() - timedelta(days=retention_days)
    pruned: list[str] = []
    for path in archive_files(logs_dir):
        day = _archive_date(path.name)
        if day is not None and day < cutoff:
            path.unlink()
            pruned.append(path.name)
    return pruned


def rotate_logs(home: Path, logs_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Roll the active log over if it's a new day or over the size cap, then
    prune archives past the retention window. Returns what happened. A no-op
    (and cheap) on the common path where neither trigger fires."""
    config = _rotation_config(home)
    if config.get("enabled", True) is False:
        return {"rotated": None, "pruned": []}

    now = now or datetime.now(timezone.utc)
    active = logs_dir / ACTIVE_LOG
    rotated: str | None = None

    if active.exists() and active.stat().st_size > 0:
        max_bytes = int(config.get("max_bytes", DEFAULT_MAX_BYTES))
        start_day = _first_event_date(active)
        by_day = start_day is not None and start_day != now.date()
        by_size = active.stat().st_size >= max_bytes
        if by_day or by_size:
            archive = _archive_path(logs_dir, start_day or now.date())
            active.rename(archive)
            rotated = archive.name

    pruned = _prune(logs_dir, now, int(config.get("retention_days", DEFAULT_RETENTION_DAYS)))
    return {"rotated": rotated, "pruned": pruned}

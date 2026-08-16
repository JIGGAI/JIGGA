"""A durable inbox for events that arrive from outside the heartbeat.

Schedules and state triggers are *pulled* — the supervisor asks, on its own
clock, whether anything is due. A webhook is *pushed*: it arrives whenever the
provider feels like it, and the provider is waiting on an HTTP response while
you decide what to do about it.

The whole design follows from refusing to execute in the receiving path:

**Enqueue, never execute.** The listener writes a file and returns. The
supervisor drains it on the next tick. That gives a fast HTTP response (every
provider times out and retries), crash-safety (the event is on disk before the
sender is told OK), bounded concurrency (execution stays inside the tick budget
from #189), and one execution path shared with pull triggers rather than a
second one that drifts.

**Delivery is at-least-once, so dedup is mandatory.** Every webhook provider
retries; some retry aggressively on a slow response. An `idempotency_key`
derived from the payload is what stops a retry becoming a second run. This is
the same shape as the per-subject dedup in `triggers.py`, with the key coming
from the sender instead of a calendar.

**Claim before executing, and never auto-retry.** A drained event is *moved* to
`processing/` before it runs. If the process dies mid-run, the event does not
quietly re-run on restart — a half-executed side effect (a message half-sent,
an order half-placed) must not be blindly repeated. The stale entry is swept
into `failed/` where it is visible and a human decides. This mirrors exactly
what `recovery.py` does for tasks and nodes.

**Bounded.** A burst that outruns the drain rate must not fill the disk. Past
`max_pending` the queue rejects and says so, rather than silently growing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from jigga.core.config import load_runtime_config
from jigga.core.io import ensure_dir, read_json, write_json
from jigga.core.models import now_iso
from jigga.core.paths import JiggaPaths
from jigga.runtime.audit import append_event, new_id

# Cap on undrained events. Sized so a burst survives a few missed ticks without
# letting a runaway sender fill the disk.
DEFAULT_MAX_PENDING = 500
# How long a claimed event may sit in `processing/` before it is considered
# orphaned by a crash. Deliberately generous — a long workflow is not a fault.
DEFAULT_PROCESSING_STALE_MINUTES = 60
# How long a delivered idempotency key is remembered, so a provider retrying
# well after the fact still can't double-fire.
DEFAULT_DEDUP_RETENTION_HOURS = 48


class QueueFull(RuntimeError):
    """The pending queue is at capacity; the sender should retry later."""


def _root(paths: JiggaPaths) -> Path:
    return paths.home / "events"


def _pending(paths: JiggaPaths) -> Path:
    return _root(paths) / "pending"


def _processing(paths: JiggaPaths) -> Path:
    return _root(paths) / "processing"


def _failed(paths: JiggaPaths) -> Path:
    return _root(paths) / "failed"


def _seen_path(paths: JiggaPaths) -> Path:
    return _root(paths) / "delivered.json"


def _settings(paths: JiggaPaths) -> dict[str, Any]:
    return (load_runtime_config(paths.home).get("events") or {})


def max_pending(paths: JiggaPaths) -> int:
    try:
        return int(_settings(paths).get("max_pending", DEFAULT_MAX_PENDING))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PENDING


def _parse(stamp: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- dedup -------------------------------------------------------------------


def _load_seen(paths: JiggaPaths) -> dict[str, str]:
    path = _seen_path(paths)
    if not path.exists():
        return {}
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _remember(paths: JiggaPaths, key: str, now: datetime) -> None:
    seen = _load_seen(paths)
    cutoff = now - timedelta(hours=DEFAULT_DEDUP_RETENTION_HOURS)
    seen = {k: v for k, v in seen.items() if (_parse(v) or now) >= cutoff}
    seen[key] = now.isoformat()
    ensure_dir(_root(paths))
    write_json(_seen_path(paths), seen)


def already_delivered(paths: JiggaPaths, key: str) -> bool:
    return key in _load_seen(paths)


# --- writing -----------------------------------------------------------------


def enqueue(paths: JiggaPaths, *, source: str, kind: str, payload: dict[str, Any],
            idempotency_key: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Accept an event for later execution.

    Returns the stored record, or `{"status": "duplicate"}` when the
    idempotency key has been seen. Raises `QueueFull` at capacity — the caller
    should translate that into a retryable response, because dropping silently
    would lose the event with the sender believing it landed.
    """
    current = now or datetime.now(timezone.utc)
    if idempotency_key and already_delivered(paths, idempotency_key):
        append_event(paths.logs, "event.duplicate", source=source, kind=kind,
                     idempotency_key=idempotency_key)
        return {"status": "duplicate", "idempotency_key": idempotency_key}

    ensure_dir(_pending(paths))
    if len(list(_pending(paths).glob("*.json"))) >= max_pending(paths):
        append_event(paths.logs, "event.rejected", status="error", source=source, kind=kind,
                     reason="queue full", max_pending=max_pending(paths))
        raise QueueFull(
            f"event queue is at capacity ({max_pending(paths)} pending) — retry later")

    record = {
        "id": new_id("inevt"),
        "source": source,
        "kind": kind,
        "payload": payload,
        "idempotency_key": idempotency_key,
        "received_at": current.isoformat(),
    }
    # Timestamp-prefixed so a plain sorted glob drains in arrival order.
    name = f"{current.strftime('%Y%m%dT%H%M%S%f')}-{record['id']}.json"
    write_json(_pending(paths) / name, record)
    if idempotency_key:
        _remember(paths, idempotency_key, current)
    append_event(paths.logs, "event.received", source=source, kind=kind, event_id=record["id"],
                 idempotency_key=idempotency_key)
    return {"status": "accepted", **record}


# --- draining ----------------------------------------------------------------


def pending_count(paths: JiggaPaths) -> int:
    directory = _pending(paths)
    return len(list(directory.glob("*.json"))) if directory.exists() else 0


def claim(paths: JiggaPaths, limit: int) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Move up to `limit` pending events into `processing/` and yield them.

    The move happens *before* the caller runs anything. A crash then leaves a
    visible claimed entry rather than an event that silently replays — the same
    at-most-once posture tasks have, and for the same reason: a half-executed
    side effect must not be blindly repeated.
    """
    directory = _pending(paths)
    if not directory.exists():
        return
    ensure_dir(_processing(paths))
    for path in sorted(directory.glob("*.json"))[:limit]:
        target = _processing(paths) / path.name
        try:
            path.rename(target)          # atomic on the same filesystem
            record = read_json(target)
        except (OSError, ValueError) as exc:
            append_event(paths.logs, "event.claim_failed", status="error",
                         file=path.name, error=str(exc))
            continue
        record["claimed_at"] = now_iso()
        write_json(target, record)
        yield target, record


def complete(paths: JiggaPaths, path: Path) -> None:
    path.unlink(missing_ok=True)


def fail(paths: JiggaPaths, path: Path, error: str) -> None:
    """Park a failed event where a human can see it. Never re-queued
    automatically — whether a partially-applied effect is safe to repeat is not
    a decision the runtime can make."""
    ensure_dir(_failed(paths))
    try:
        record = read_json(path)
    except (OSError, ValueError):
        record = {"id": path.stem}
    record["error"] = error
    record["failed_at"] = now_iso()
    write_json(_failed(paths) / path.name, record)
    path.unlink(missing_ok=True)
    append_event(paths.logs, "event.failed", status="error", event_id=record.get("id"),
                 source=record.get("source"), kind=record.get("kind"), error=error)


def sweep_stale_processing(paths: JiggaPaths, *, now: datetime | None = None) -> list[str]:
    """Claimed events stranded by a crash → `failed/`, with an audit event.

    Visible, never silently retried — same contract as `recovery.sweep_stale`.
    """
    directory = _processing(paths)
    if not directory.exists():
        return []
    current = now or datetime.now(timezone.utc)
    try:
        minutes = int(_settings(paths).get("processing_stale_minutes",
                                           DEFAULT_PROCESSING_STALE_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_PROCESSING_STALE_MINUTES
    cutoff = current - timedelta(minutes=minutes)
    swept: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            record = read_json(path)
        except (OSError, ValueError):
            record = {}
        claimed = _parse(record.get("claimed_at"))
        if claimed is not None and claimed >= cutoff:
            continue
        fail(paths, path, "interrupted — claimed but never completed (side effects unknown)")
        swept.append(str(record.get("id") or path.stem))
    return swept


def list_failed(paths: JiggaPaths) -> list[dict[str, Any]]:
    directory = _failed(paths)
    if not directory.exists():
        return []
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            records.append(read_json(path))
        except (OSError, ValueError):
            records.append({"id": path.stem, "error": "unreadable record"})
    return records


def stats(paths: JiggaPaths) -> dict[str, int]:
    def _count(directory: Path) -> int:
        return len(list(directory.glob("*.json"))) if directory.exists() else 0

    return {
        "pending": _count(_pending(paths)),
        "processing": _count(_processing(paths)),
        "failed": _count(_failed(paths)),
    }


def as_json(paths: JiggaPaths) -> str:
    return json.dumps(stats(paths), indent=2)

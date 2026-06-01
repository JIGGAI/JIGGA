"""Per-agent spend ledger — a derived, incremental index over the audit log
(Hardening H1a).

Budget enforcement needs an agent's spend-within-window on *every* model call.
Computing that with `cost.agent_spend` re-reads the entire audit log (all
rotated archives + the active file) and re-parses every line, per call — O(history)
on the hot path of an agent's tool-use loop.

This module keeps a small per-agent ledger under `state/spend/<agent>.json` that
**tails** the active `events.jsonl` by byte offset: each budget check reads only
the bytes appended since last time, folds matching `model.call` costs in, and
prunes to the rolling window. The audit log stays the single source of truth —
the ledger holds no spend the log doesn't. If the active log is shorter than the
recorded offset (it was unlinked, truncated, or rotated), the ledger rebuilds
from a full read (archives + active) and resets its offset. So clearing the log
clears the derived spend, and rotation is self-healing.

Reporting paths (`cost.cost_summary`, `budget_status`) intentionally keep using
the full scan — they're cold and want the complete picture.

Invariant: an agent's budget window is fixed (it comes from config). The ledger
drops entries that age out of a rolling window — correct, since time only moves
forward. Widening a window later (e.g. `30d` → `all`) is a config change that the
ledger won't retroactively recover already-pruned spend for; rebuild by clearing
`state/spend/<agent>.json` if that matters.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jigga.core.io import read_json, write_json
from jigga.runtime.audit_query import (
    _event_time,
    events_path,
    parse_since,
    read_events,
)

# Windows that mean "no time filter — count everything" (mirrors cost.py).
_ALL_WINDOW = {None, "", "all", "0"}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _ledger_path(home: Path, agent_id: str) -> Path:
    safe = _SAFE_NAME.sub("_", agent_id) or "_"
    # Sanitizing distinct ids (e.g. "a/b" and "a_b") can collide on one file,
    # which would let one agent's ledger mask another's spend. Disambiguate with
    # a hash of the original id whenever sanitization changed anything.
    if safe != agent_id:
        safe = f"{safe}.{hashlib.sha1(agent_id.encode('utf-8')).hexdigest()[:10]}"
    return Path(home) / "state" / "spend" / f"{safe}.json"


def _empty() -> dict[str, Any]:
    return {"offset": 0, "head": "", "carried": 0.0, "entries": []}


def _num(value: Any) -> float:
    """Coerce a cost to a finite float; malformed / NaN / inf → 0.0. A corrupt
    cost_usd in the log must not crash budget enforcement or poison the sum."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return n if math.isfinite(n) else 0.0


def _cutoff(window: str | None, now: datetime | None) -> datetime | None:
    return None if window in _ALL_WINDOW else parse_since(window, now=now)


def _active_head(active: Path) -> str:
    """First line of the active log — a cheap signature of the file's identity.
    If it changes, the file was rotated/replaced and the stored byte offset is
    no longer valid for it."""
    if not active.exists():
        return ""
    try:
        with active.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.readline(256)
    except OSError:
        return ""


def _cost_for(line: str, agent_id: str) -> tuple[str, float] | None:
    """Parse one audit line; return (time, cost) if it's this agent's priced
    model.call, else None."""
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if event.get("type") != "model.call":
        return None
    details = event.get("details") or {}
    if "cost_usd" not in details or str(details.get("agent_id")) != agent_id:
        return None
    return str(event.get("time", "")), _num(details.get("cost_usd"))


def _rebuild(home: Path, logs_dir: Path, agent_id: str) -> dict[str, Any]:
    """Recompute the ledger from the full audit log (archives + active). Sets
    the offset to the active log's current size so future reads only tail."""
    data = _empty()
    for event in read_events(logs_dir):
        if event.get("type") != "model.call":
            continue
        details = event.get("details") or {}
        if "cost_usd" not in details or str(details.get("agent_id")) != agent_id:
            continue
        data["entries"].append([str(event.get("time", "")), _num(details.get("cost_usd"))])
    active = events_path(logs_dir)
    data["offset"] = active.stat().st_size if active.exists() else 0
    data["head"] = _active_head(active)
    return data


def _tail(data: dict[str, Any], home: Path, logs_dir: Path, agent_id: str) -> dict[str, Any]:
    """Fold audit lines appended past the recorded offset into the ledger. Falls
    back to a full rebuild when the active log shrank OR its head line changed —
    both mean rotation/truncation, after which the stored offset points into the
    wrong file (which would silently drop or double-count spend)."""
    active = events_path(logs_dir)
    size = active.stat().st_size if active.exists() else 0
    offset = data.get("offset", 0)
    head = _active_head(active)
    if size < offset or (offset > 0 and head != data.get("head", "")):
        return _rebuild(home, logs_dir, agent_id)
    if size > offset:
        with active.open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            for line in handle:
                hit = _cost_for(line, agent_id)
                if hit is not None:
                    data["entries"].append([hit[0], hit[1]])
        data["offset"] = size
    data["head"] = head
    return data


def _load_ledger(path: Path) -> dict[str, Any] | None:
    """Load a valid ledger, or None if it's missing/corrupt/wrong-shape (the
    caller then does a full rebuild — so a clobbered ledger self-heals rather
    than crashing or silently under-counting)."""
    if not path.exists():
        return None
    try:
        data = read_json(path)
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None
    try:
        offset = int(data.get("offset", 0))
    except (TypeError, ValueError):
        return None
    if offset < 0:
        return None
    data["offset"] = offset
    data["carried"] = _num(data.get("carried", 0.0))
    data["head"] = str(data.get("head", ""))
    return data


def _prune(data: dict[str, Any], cutoff: datetime | None) -> None:
    """Drop entries outside the rolling window. With no window (count-all), fold
    entries into a running `carried` total so the on-disk list stays bounded."""
    if cutoff is None:
        data["carried"] = round(data.get("carried", 0.0) + sum(_num(c) for _, c in data["entries"]), 6)
        data["entries"] = []
        return
    kept: list[list[Any]] = []
    for time_iso, cost in data["entries"]:
        parsed = _event_time({"time": time_iso})
        if parsed is not None and parsed >= cutoff:
            kept.append([time_iso, cost])
    data["entries"] = kept


def window_spend(
    home: Path,
    logs_dir: Path,
    agent_id: str,
    *,
    window: str | None,
    now: datetime | None = None,
) -> float:
    """Agent's spend within `window`, read from the derived ledger (tailing the
    audit log incrementally rather than re-scanning it whole)."""
    path = _ledger_path(home, agent_id)
    data = _load_ledger(path)
    # Missing or corrupt ledger → full rebuild from the audit log (archives +
    # active), not a tail-from-zero (which would miss already-rotated spend).
    data = _rebuild(home, logs_dir, agent_id) if data is None else _tail(data, home, logs_dir, agent_id)
    _prune(data, _cutoff(window, now))
    write_json(path, data)
    return round(data.get("carried", 0.0) + sum(_num(c) for _, c in data["entries"]), 6)

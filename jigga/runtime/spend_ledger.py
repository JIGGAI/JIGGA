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

import json
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
    return Path(home) / "state" / "spend" / f"{safe}.json"


def _empty() -> dict[str, Any]:
    return {"offset": 0, "carried": 0.0, "entries": []}


def _cutoff(window: str | None, now: datetime | None) -> datetime | None:
    return None if window in _ALL_WINDOW else parse_since(window, now=now)


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
    return str(event.get("time", "")), float(details.get("cost_usd") or 0.0)


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
        data["entries"].append([str(event.get("time", "")), float(details.get("cost_usd") or 0.0)])
    active = events_path(logs_dir)
    data["offset"] = active.stat().st_size if active.exists() else 0
    return data


def _tail(data: dict[str, Any], home: Path, logs_dir: Path, agent_id: str) -> dict[str, Any]:
    """Fold any audit lines appended past the recorded offset into the ledger.
    Rebuilds from scratch if the active log shrank (unlink / truncate / rotate)."""
    active = events_path(logs_dir)
    size = active.stat().st_size if active.exists() else 0
    if size < data.get("offset", 0):
        return _rebuild(home, logs_dir, agent_id)
    if size > data.get("offset", 0):
        with active.open("r", encoding="utf-8") as handle:
            handle.seek(data["offset"])
            for line in handle:
                hit = _cost_for(line, agent_id)
                if hit is not None:
                    data["entries"].append([hit[0], hit[1]])
        data["offset"] = size
    return data


def _prune(data: dict[str, Any], cutoff: datetime | None) -> None:
    """Drop entries outside the rolling window. With no window (count-all), fold
    entries into a running `carried` total so the on-disk list stays bounded."""
    if cutoff is None:
        data["carried"] = round(data.get("carried", 0.0) + sum(c for _, c in data["entries"]), 6)
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
    data = read_json(path) if path.exists() else _empty()
    data = _tail(data, home, logs_dir, agent_id)
    _prune(data, _cutoff(window, now))
    write_json(path, data)
    return round(data.get("carried", 0.0) + sum(c for _, c in data["entries"]), 6)

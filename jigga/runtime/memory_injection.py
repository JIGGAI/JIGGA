"""What the context pack actually injects, measured over time.

JIGGA's side of ClawRecipes ticket 0272. Before deciding whether a knowledge
graph is worth building (`docs/MEMORY_BACKENDS.md`), the question to answer with
numbers rather than intuition is: how much does the current fixed-cap,
recency-first strategy inject, and how much relevance does it drop on the floor?

The data source is the audit log — `agent.context.assembled` already fired once
per agent run and now carries sizes. No second log tree, no parallel format: the
events are already durable, already rotated, already queryable by `jigga audit`.

Nothing here changes what gets injected. It reports.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _percentile(values: list[int], pct: float) -> int:
    """Nearest-rank percentile. Exact for the small samples this reports on, and
    without pulling in a stats dependency for four numbers."""
    if not values:
        return 0
    import math

    ordered = sorted(values)
    # ceil, not round: nearest-rank is defined that way, and round() would put
    # p50 of five samples on the second value instead of the middle one.
    rank = max(1, min(len(ordered), math.ceil(pct / 100 * len(ordered))))
    return ordered[rank - 1]


def read_events(logs_dir: Path, *, days: int) -> list[dict[str, Any]]:
    """`agent.context.assembled` events from the last `days` days.

    Events written before this instrumentation have no `chars` field; they are
    skipped rather than counted as zero, which would drag every percentile
    toward a baseline that was never measured.
    """
    path = Path(logs_dir) / "events.jsonl"
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if '"agent.context.assembled"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a torn final line must not lose the rest of the report
        details = event.get("details") or {}
        if "chars" not in details:
            continue
        try:
            when = datetime.fromisoformat(str(event.get("time", "")))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            events.append(event)
    return events


def report(logs_dir: Path, *, days: int = 7) -> dict[str, Any]:
    """The weekly baseline: what was injected, what was dropped, and by whom."""
    events = read_events(logs_dir, days=days)
    if not events:
        return {"days": days, "runs": 0, "note": "No measured context assemblies in this window."}

    details = [e.get("details") or {} for e in events]
    tokens = [int(d.get("tokens_est") or 0) for d in details]
    capped = [d for d in details if d.get("hit_total_cap")]

    # Per-layer: how often a layer is present, clipped by its cap, or dropped
    # entirely because the total ran out before it.
    layers: dict[str, dict[str, int]] = {}
    for d in details:
        for layer in d.get("layer_detail") or []:
            row = layers.setdefault(str(layer.get("name")),
                                    {"seen": 0, "clipped": 0, "dropped": 0,
                                     "chars_available": 0, "chars_included": 0})
            row["seen"] += 1
            row["clipped"] += 1 if layer.get("clipped") else 0
            row["dropped"] += 1 if layer.get("dropped") else 0
            row["chars_available"] += int(layer.get("available") or 0)
            row["chars_included"] += int(layer.get("included") or 0)

    by_agent: dict[str, dict[str, int]] = {}
    for e, d in zip(events, details, strict=True):
        agent = str((e.get("details") or {}).get("agent") or "?")
        row = by_agent.setdefault(agent, {"runs": 0, "tokens": 0})
        row["runs"] += 1
        row["tokens"] += int(d.get("tokens_est") or 0)

    stable = sum(int(d.get("stable_chars") or 0) for d in details)
    volatile = sum(int(d.get("volatile_chars") or 0) for d in details)

    return {
        "days": days,
        "runs": len(events),
        "tokens_est": {
            "p50": _percentile(tokens, 50),
            "p95": _percentile(tokens, 95),
            "max": max(tokens),
            "mean": round(sum(tokens) / len(tokens)),
        },
        "hit_total_cap": {"runs": len(capped), "pct": round(100 * len(capped) / len(events), 1)},
        # The cache lever: stable layers are byte-identical across calls and so
        # are eligible for the provider's prompt cache; volatile bytes are
        # re-billed at full price every single call.
        "cacheable_split": {
            "stable_chars": stable,
            "volatile_chars": volatile,
            "stable_pct": round(100 * stable / (stable + volatile), 1) if stable + volatile else 0.0,
        },
        "recency_window": {
            "pinned_available": sum(int(d.get("pinned_available") or 0) for d in details),
            "pinned_included": sum(int(d.get("pinned_included") or 0) for d in details),
            "facts_available": sum(int(d.get("facts_available") or 0) for d in details),
            "facts_included": sum(int(d.get("facts_included") or 0) for d in details),
        },
        "layers": dict(sorted(layers.items(), key=lambda kv: -kv[1]["chars_included"])),
        "by_agent": dict(sorted(by_agent.items(), key=lambda kv: -kv[1]["tokens"])),
        "restricted_runs": sum(1 for d in details if d.get("restricted")),
    }


def render(data: dict[str, Any]) -> str:
    """The same report as text, for someone reading it in a terminal."""
    if not data.get("runs"):
        return str(data.get("note", "No data."))
    lines = [f"Context injection over {data['days']}d — {data['runs']} agent runs", ""]
    t = data["tokens_est"]
    lines.append(f"  tokens injected   p50 {t['p50']}   p95 {t['p95']}   max {t['max']}   mean {t['mean']}")
    cap = data["hit_total_cap"]
    lines.append(f"  hit the 9k total cap   {cap['runs']} runs ({cap['pct']}%)")
    split = data["cacheable_split"]
    lines.append(f"  prefix-cacheable   {split['stable_pct']}% of injected chars "
                 f"({split['stable_chars']} stable / {split['volatile_chars']} volatile)")
    rec = data["recency_window"]
    lines.append(f"  team facts seen    pinned {rec['pinned_included']}/{rec['pinned_available']} · "
                 f"learnings {rec['facts_included']}/{rec['facts_available']}")
    lines.append("")
    lines.append(f"  {'layer':<16}{'runs':>6}{'clipped':>9}{'dropped':>9}{'chars in':>11}{'of':>11}")
    for name, row in data["layers"].items():
        lines.append(f"  {name:<16}{row['seen']:>6}{row['clipped']:>9}{row['dropped']:>9}"
                     f"{row['chars_included']:>11}{row['chars_available']:>11}")
    lines.append("")
    lines.append("  busiest agents:")
    for agent, row in list(data["by_agent"].items())[:5]:
        lines.append(f"    {agent:<24}{row['runs']:>5} runs  {row['tokens']:>8} tokens")
    return "\n".join(lines)

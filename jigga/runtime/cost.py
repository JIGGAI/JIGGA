"""Model cost accounting and per-agent budgets (Milestone C).

Every model call records its token usage and dollar cost on the `model.call`
audit event. This module is the math and the read side: price a call from a
config rate table, roll spend up per agent over a time window, and resolve an
agent's budget into an ok / warn / exceeded status. Enforcement lives in
`model_router.call_model` (the single chokepoint for model spend); it uses
`agent_budget` + `agent_spend` from here to hard-stop at the cap and the
`WARN_FRACTION` threshold to soft-warn on the way up.

Pricing and budgets are config, not code:

    models:
      pricing:
        gpt-4o:        {input_per_1k: 0.005, output_per_1k: 0.015}
        default:       {input_per_1k: 0.0,   output_per_1k: 0.0}
    budgets:
      window: 30d                # rolling window for every cap (default 30d)
      agents:
        marketing_lead: {limit_usd: 10.0}
      default: {limit_usd: 5.0}  # applies to any agent without its own entry
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from jigga.core.config import load_runtime_config
from jigga.runtime.audit_query import _event_time, parse_since, read_events

# Soft-warn once cumulative spend crosses this fraction of the cap.
WARN_FRACTION = 0.8

# Windows that mean "no time filter — count everything".
_ALL_WINDOW = {None, "", "all", "0"}


def _coerce_cost(value: Any) -> float:
    """A cost from the log coerced to a finite float; malformed / NaN / inf → 0.0
    so a single corrupt `cost_usd` line can't crash a rollup or poison the sum."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    return n if math.isfinite(n) else 0.0


def estimate_tokens(text: str) -> int:
    """Rough token count for providers that don't return usage (e.g. dry-run).

    The ~4-characters-per-token heuristic is good enough for budget accounting;
    real providers report exact usage and override this."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def load_pricing(home: Path) -> dict[str, Any]:
    models = load_runtime_config(home).get("models") or {}
    return dict(models.get("pricing") or {})


def price_call(model: str, input_tokens: int, output_tokens: int, pricing: dict[str, Any]) -> float:
    """Dollar cost of one call. Falls back to a `default` rate, then to free."""
    rate = pricing.get(model) or pricing.get("default") or {}
    in_rate = float(rate.get("input_per_1k", 0.0) or 0.0)
    out_rate = float(rate.get("output_per_1k", 0.0) or 0.0)
    return round(input_tokens / 1000 * in_rate + output_tokens / 1000 * out_rate, 6)


def load_budgets(home: Path) -> dict[str, Any]:
    return dict(load_runtime_config(home).get("budgets") or {})


def agent_budget(home: Path, agent_id: str) -> dict[str, Any] | None:
    """Resolve the budget that governs `agent_id` — its own entry, else the
    `default`. Returns None when no cap applies (the common case: budgets are
    opt-in). A spec with no `limit_usd` is treated as no cap."""
    budgets = load_budgets(home)
    spec = (budgets.get("agents") or {}).get(agent_id)
    if spec is None:
        spec = budgets.get("default")
    if not spec or spec.get("limit_usd") is None:
        return None
    resolved = dict(spec)
    resolved.setdefault("window", budgets.get("window", "30d"))
    return resolved


def _cost_events(
    logs_dir: Path, *, since: str | None = None, now: datetime | None = None
) -> Iterator[dict[str, Any]]:
    cutoff = parse_since(since, now=now) if since not in _ALL_WINDOW else None
    for event in read_events(logs_dir):
        if event.get("type") != "model.call":
            continue
        details = event.get("details") or {}
        if "cost_usd" not in details:
            continue
        if cutoff is not None:
            event_time = _event_time(event)
            if event_time is None or event_time < cutoff:
                continue
        yield details


def agent_spend(
    logs_dir: Path, agent_id: str, *, since: str | None = None, now: datetime | None = None
) -> float:
    total = 0.0
    for details in _cost_events(logs_dir, since=since, now=now):
        if str(details.get("agent_id")) == agent_id:
            total += _coerce_cost(details.get("cost_usd"))
    return round(total, 6)


def cost_summary(
    logs_dir: Path, *, since: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Per-agent rollup of model spend, plus a grand total. Agents are sorted
    by cost descending."""
    rows: dict[str, dict[str, Any]] = {}
    for details in _cost_events(logs_dir, since=since, now=now):
        agent = str(details.get("agent_id") or "unknown")
        row = rows.setdefault(
            agent, {"agent": agent, "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        )
        row["calls"] += 1
        row["input_tokens"] += int(details.get("input_tokens") or 0)
        row["output_tokens"] += int(details.get("output_tokens") or 0)
        row["cost_usd"] = round(row["cost_usd"] + _coerce_cost(details.get("cost_usd")), 6)
    agents = sorted(rows.values(), key=lambda r: r["cost_usd"], reverse=True)
    total = {
        "calls": sum(r["calls"] for r in agents),
        "input_tokens": sum(r["input_tokens"] for r in agents),
        "output_tokens": sum(r["output_tokens"] for r in agents),
        "cost_usd": round(sum(r["cost_usd"] for r in agents), 6),
    }
    return {"agents": agents, "total": total}


def budget_status(
    home: Path, logs_dir: Path, agent_id: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """An agent's cap, spend-to-date in its window, and ok/warn/exceeded state.
    None when the agent has no budget configured."""
    budget = agent_budget(home, agent_id)
    if budget is None:
        return None
    limit = float(budget["limit_usd"])
    window = budget.get("window")
    since = None if window in _ALL_WINDOW else window
    spent = agent_spend(logs_dir, agent_id, since=since, now=now)
    fraction = (spent / limit) if limit > 0 else 0.0
    if spent >= limit:
        state = "exceeded"
    elif fraction >= WARN_FRACTION:
        state = "warn"
    else:
        state = "ok"
    return {
        "agent": agent_id,
        "limit_usd": limit,
        "spent_usd": spent,
        "remaining_usd": round(max(0.0, limit - spent), 6),
        "fraction": round(fraction, 4),
        "window": window,
        "state": state,
    }

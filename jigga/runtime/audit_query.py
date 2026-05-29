"""Reading the audit log — the query side of observability.

The runtime writes a JSONL audit event at every trust boundary
(`agent.run.*`, `capability.invocation.*`, `agent.tool_call.*`,
`policy.denied`, `channel.message.received`, `subagent.spawn.*`, ...). This
module reads `~/.jigga/logs/events.jsonl` back with filtering, tailing, and
id-correlation, backing the `jigga logs` / `jigga audit` / `jigga trace` CLI.

Until trace-id propagation lands (a follow-up), `trace(id)` correlates by any
id-shaped value an event carries (run_id / task_id / session_id / the event's
own id), which already stitches together most causal chains.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DURATION = re.compile(r"^(\d+)\s*([smhdw])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def events_path(logs_dir: Path) -> Path:
    return logs_dir / "events.jsonl"


def read_events(logs_dir: Path) -> list[dict[str, Any]]:
    path = events_path(logs_dir)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def parse_since(value: str, *, now: datetime | None = None) -> datetime:
    """Parse a `--since` argument into a cutoff datetime.

    Accepts a relative duration (`30m`, `24h`, `7d`, `2w`, `90s`) or an ISO
    timestamp. Raises ValueError on anything else.
    """
    now = now or datetime.now(timezone.utc)
    text = value.strip()
    match = _DURATION.match(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        return now - timedelta(seconds=amount * _UNIT_SECONDS[unit])
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid --since {value!r}: use e.g. 30m / 24h / 7d or an ISO timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _event_time(event: dict[str, Any]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(event.get("time", "")))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _matches_agent(event: dict[str, Any], agent: str) -> bool:
    details = event.get("details") or {}
    return agent in {str(details.get("agent")), str(details.get("agent_id")), str(details.get("parent_agent_id"))}


def _matches_type(event: dict[str, Any], type_filter: str) -> bool:
    etype = str(event.get("type", ""))
    # Trailing-dot or bare prefix matches a family: "agent.tool_call" matches
    # "agent.tool_call.executed". Exact match also works.
    return etype == type_filter or etype.startswith(type_filter + ".") or etype.startswith(type_filter)


def query_events(
    logs_dir: Path,
    *,
    agent: str | None = None,
    type_filter: str | None = None,
    since: str | None = None,
    status: str | None = None,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Filter the audit log. Results are chronological; `limit` keeps the most
    recent N (the tail)."""
    events = read_events(logs_dir)
    cutoff = parse_since(since, now=now) if since else None

    selected: list[dict[str, Any]] = []
    for event in events:
        if cutoff is not None:
            t = _event_time(event)
            if t is None or t < cutoff:
                continue
        if type_filter and not _matches_type(event, type_filter):
            continue
        if agent and not _matches_agent(event, agent):
            continue
        if status and str(event.get("status")) != status:
            continue
        selected.append(event)

    if limit is not None and limit >= 0:
        selected = selected[-limit:]
    return selected


def tail_events(logs_dir: Path, count: int = 20) -> list[dict[str, Any]]:
    events = read_events(logs_dir)
    return events[-count:] if count >= 0 else events


def trace(logs_dir: Path, identifier: str) -> list[dict[str, Any]]:
    """Return events correlated to an id — the event's own id, or any
    id-shaped value in its details (run_id / task_id / session_id /
    trace_id / parent ids). Matches the identifier as an exact value or as a
    prefix (so a short id prefix works like elsewhere in the CLI)."""
    matched: list[dict[str, Any]] = []
    for event in read_events(logs_dir):
        if _event_references(event, identifier):
            matched.append(event)
    return matched


def _event_references(event: dict[str, Any], identifier: str) -> bool:
    def _hit(value: Any) -> bool:
        if isinstance(value, str):
            return value == identifier or value.startswith(identifier)
        return False

    if _hit(str(event.get("id", ""))):
        return True
    details = event.get("details") or {}
    correlation_keys = (
        "run_id", "task_id", "session_id", "trace_id", "parent_session",
        "id", "workflow", "agent", "agent_id",
    )
    for key in correlation_keys:
        if key in details and _hit(str(details[key])):
            return True
    return False


def format_event(event: dict[str, Any]) -> str:
    """One-line human rendering: <time> <status?> <type> <key details>."""
    time = str(event.get("time", ""))[:19]
    etype = event.get("type", "?")
    status = event.get("status", "ok")
    details = event.get("details") or {}
    # Show a compact, useful subset of details inline.
    keys = ("agent", "agent_id", "task_id", "action", "capability", "channel",
            "workflow", "run_id", "reason", "error", "backend")
    shown = " ".join(f"{k}={details[k]}" for k in keys if details.get(k) not in (None, ""))
    flag = "" if status == "ok" else f"[{status}] "
    return f"{time}  {flag}{etype}  {shown}".rstrip()

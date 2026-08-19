"""Reading the audit log — the query side of observability.

The runtime writes a JSONL audit event at every trust boundary
(`agent.run.*`, `capability.invocation.*`, `agent.tool_call.*`,
`policy.denied`, `channel.message.received`, `subagent.spawn.*`, ...). This
module reads `~/.jigga/logs/events.jsonl` back with filtering, tailing, and
id-correlation, backing the `jigga logs` / `jigga audit` / `jigga trace` CLI.

`trace(id)` correlates by any id-shaped value an event carries — the ambient
`trace_id` propagated across a whole operation (supervisor tick → agent run →
subagent), or a narrower `run_id` / `task_id` / `session_id` / the event's own
id when you want to follow a single run.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jigga.core.io import read_jsonl

_DURATION = re.compile(r"^(\d+)\s*([smhdw])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def events_path(logs_dir: Path) -> Path:
    return logs_dir / "events.jsonl"


def _log_files(logs_dir: Path) -> list[Path]:
    """Dated archives (oldest first) followed by the active log, so readers see
    the full retained history across rotations — not just the current day."""
    archives = sorted(
        p for p in logs_dir.glob("events-*.jsonl")
        if re.match(r"^events-\d{4}-\d{2}-\d{2}(?:\.\d+)?\.jsonl$", p.name)
    )
    active = events_path(logs_dir)
    return [*archives, *([active] if active.exists() else [])]


def read_events(logs_dir: Path) -> list[dict[str, Any]]:
    """All audit events across rotated archives + the active log, oldest first."""
    events: list[dict[str, Any]] = []
    for path in _log_files(logs_dir):
        events.extend(read_jsonl(path))
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


def _matches_actor(event: dict[str, Any], actor: str) -> bool:
    """Exact actor, or a prefix family: `agent` matches every `agent:<id>`,
    `user` matches `user` and every `user:<channel>`. `human` is the one people
    actually want — everything a person did, however they reached JIGGA."""
    value = str(event.get("actor") or "system")
    if actor == "human":
        return value == "user" or value.startswith("user:")
    if actor == "machine":
        return not (value == "user" or value.startswith("user:"))
    return value == actor or value.startswith(f"{actor}:")


def _matches_text(event: dict[str, Any], needle: str) -> bool:
    """Case-insensitive substring match over the whole event.

    Searching the serialised event rather than a chosen set of fields, because
    the useful needle is usually in `details` — an error sentence, a file path,
    a conversation id — and which key holds it differs per event type. A search
    that only looked at `type` would miss every one of those.
    """
    return needle in json.dumps(event, default=str).lower()


def query_events(
    logs_dir: Path,
    *,
    agent: str | None = None,
    type_filter: str | None = None,
    since: str | None = None,
    status: str | None = None,
    actor: str | None = None,
    contains: str | None = None,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Filter the audit log. Results are chronological; `limit` keeps the most
    recent N (the tail)."""
    events = read_events(logs_dir)
    cutoff = parse_since(since, now=now) if since else None
    needle = (contains or "").strip().lower()

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
        if actor and not _matches_actor(event, actor):
            continue
        if needle and not _matches_text(event, needle):
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
    actor = event.get("actor") or "system"
    details = event.get("details") or {}
    # Show a compact, useful subset of details inline.
    keys = ("agent", "agent_id", "task_id", "action", "capability", "channel",
            "workflow", "run_id", "reason", "error", "backend")
    shown = " ".join(f"{k}={details[k]}" for k in keys if details.get(k) not in (None, ""))
    flag = "" if status == "ok" else f"[{status}] "
    return f"{time}  {flag}{actor}  {etype}  {shown}".rstrip()

"""Event triggers — fire a workflow because something is *true*, not because
the clock said so (#151).

Only `trigger.schedule` fired before this. A schedule answers "every weekday at
09:00"; it cannot answer "15 minutes before any meeting", because the thing
being waited on isn't a time, it's a *subject* whose time you have to go and
look up.

**Time and state are the same mechanism.** Both are evaluated on the supervisor
heartbeat, deduped, and turned into a `JiggaEvent`. A state trigger just
consults a capability instead of a clock. That symmetry is deliberate: push
triggers (webhooks) will land on this same path rather than growing a second
execution route that drifts from the first.

Two things here are load-bearing and easy to get wrong:

**Dedup is per-subject, not per-minute.** "15m before any meeting" is true on
*every* tick inside that 15-minute window. With cron's time-bucketed dedup a
60-second heartbeat would fire it fifteen times. The key is
`(workflow, trigger, subject)` — this specific calendar event — and it is
remembered for a retention window rather than a minute.

**Evaluating a trigger spends someone's credentials.** Reading a calendar means
acting as an agent that can read that calendar, so an event trigger must name
its agent explicitly. Falling back to "the first node's agent" would make
credential use implicit and surprising; refusing is the safe reading, and the
refusal is reported rather than silently skipped.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from jigga.core.models import WorkflowConfig
from jigga.core.paths import JiggaPaths
from jigga.runtime.events import JiggaEvent

# How long a fired (workflow, trigger, subject) key is remembered. Long enough
# that a subject can't re-fire after drifting out of and back into a window;
# short enough that the loop-state file stays bounded.
FIRED_RETENTION_HOURS = 72

_OFFSET = re.compile(r"^(\d+)\s*(m|min|mins|minutes|h|hr|hrs|hours|d|days?)$", re.IGNORECASE)


class TriggerError(ValueError):
    """A trigger is declared in a way that cannot be evaluated."""


def parse_offset(raw: Any) -> timedelta:
    """`15m` / `2h` / `1d` → timedelta. Bare numbers are minutes."""
    if raw is None:
        return timedelta(0)
    if isinstance(raw, (int, float)):
        return timedelta(minutes=float(raw))
    match = _OFFSET.match(str(raw).strip())
    if not match:
        raise TriggerError(f"unparseable trigger offset {raw!r} — use forms like '15m', '2h', '1d'")
    value, unit = int(match.group(1)), match.group(2).lower()
    if unit.startswith("m"):
        return timedelta(minutes=value)
    if unit.startswith("h"):
        return timedelta(hours=value)
    return timedelta(days=value)


# --- subjects ----------------------------------------------------------------


def _as_datetime(raw: Any, *, relative_to: datetime | None = None) -> datetime | None:
    """Parse the several shapes `calendar.list_events` actually produces.

    Both shipped providers normalize to a `time` field, but not to one format:
    Google emits an ISO datetime for timed events and a bare ISO *date* for
    all-day ones, while the bundled dry-run capability emits a wall clock
    `"09:30"`. Handling only ISO datetimes — which is what I wrote first — means
    the trigger silently never fires against either provider.
    """
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Wall clock "HH:MM" → that time on the reference date. Without a date
        # of its own it can only mean today.
        match = re.match(r"^(\d{1,2}):(\d{2})$", text)
        if not match or relative_to is None:
            return None
        parsed = relative_to.replace(hour=int(match.group(1)), minute=int(match.group(2)),
                                     second=0, microsecond=0)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _matches(candidate: dict[str, Any], criteria: dict[str, Any]) -> bool:
    """Every declared key must match, case-insensitively for strings. An empty
    `match:` matches everything."""
    for key, wanted in (criteria or {}).items():
        actual = candidate.get(key)
        if isinstance(wanted, str) and isinstance(actual, str):
            if wanted.casefold() != actual.casefold():
                return False
        elif actual != wanted:
            return False
    return True


def _calendar_event_upcoming(paths: JiggaPaths, workflow: WorkflowConfig,
                             trigger: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Subjects: calendar events starting within `offset` from now.

    The window is `[now, now + offset]` — an event already begun has not "come
    up" and firing for it would be surprising (and, for a meeting-prep
    workflow, useless).
    """
    from jigga.runtime.dispatcher import RuntimeContext, dispatch_action
    from jigga.core.config import load_agents
    from jigga.core.models import WorkflowStep
    from jigga.runtime.capabilities import CapabilityRegistry

    agent_id = trigger.get("agent")
    if not agent_id:
        raise TriggerError(
            "an event trigger must name the `agent:` whose credentials evaluate it — "
            "reading a calendar acts as somebody, and that must not be implicit")
    agent = load_agents(paths.agents).get(agent_id)
    if agent is None:
        raise TriggerError(f"trigger names agent {agent_id!r}, which does not exist")

    registry = CapabilityRegistry.load(user_capabilities=paths.capabilities,
                                       approvals_dir=paths.policies)
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    step = WorkflowStep(id=f"trigger_{workflow.id}", action="calendar.list_events", input={})
    output = dispatch_action(step, {}, {}, runtime, registry, paths.logs,
                             run_id=f"trigger_{workflow.id}")

    # Two shapes ship today: the bundled dry-run capability returns a bare list,
    # the Google connector returns {events: [...]}. Accept either rather than
    # making the trigger depend on which provider is installed.
    if isinstance(output, dict):
        events = output.get("events")
    elif isinstance(output, list):
        events = output
    else:
        events = None
    if not isinstance(events, list):
        return []

    horizon = now + parse_offset(trigger.get("offset"))
    criteria = trigger.get("match") or {}
    subjects: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or not _matches(event, criteria):
            continue
        # `time` is what both providers normalize to; the others are accepted so
        # a future connector isn't forced to rename its field.
        starts = _as_datetime(event.get("time") or event.get("start") or event.get("starts_at"),
                              relative_to=now)
        if starts is None or not (now <= starts <= horizon):
            continue
        subjects.append({
            # The dry-run provider has no id, so fall back to something stable
            # per (title, start) — dedup depends on this being repeatable.
            "id": str(event.get("id") or f"{event.get('title')}@{starts.isoformat()}"),
            "payload": event,
        })
    return subjects


# The seam. An evaluator returns the *subjects* that currently satisfy the
# trigger; dedup and event construction are handled once, here, for all of them.
EVENT_EVALUATORS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "calendar_event_upcoming": _calendar_event_upcoming,
}


# --- evaluation ---------------------------------------------------------------


def fired_key(workflow_id: str, event_name: str, subject_id: str) -> str:
    return f"{workflow_id}|{event_name}|{subject_id}"


def already_fired(state: dict[str, Any], key: str) -> bool:
    return key in (state.get("event_fired") or {})


def record_fire(state: dict[str, Any], key: str, when: datetime) -> None:
    state.setdefault("event_fired", {})[key] = when.isoformat()


def prune_fired(state: dict[str, Any], now: datetime) -> None:
    """Drop keys past the retention window so loop state stays bounded."""
    fired = state.get("event_fired") or {}
    cutoff = now - timedelta(hours=FIRED_RETENTION_HOURS)
    state["event_fired"] = {
        key: stamp for key, stamp in fired.items()
        if (_as_datetime(stamp) or now) >= cutoff
    }


def due_event_triggers(paths: JiggaPaths, workflows: dict[str, WorkflowConfig],
                       state: dict[str, Any], now: datetime | None = None) -> list[JiggaEvent]:
    """Workflows whose event trigger currently matches an un-fired subject.

    Returns `workflow.event_due` events carrying the firing subject as
    `trigger_payload`, so a node can reference `${trigger.<field>}`. Never
    raises: a broken trigger produces a `workflow.trigger_error` event instead,
    because one misconfigured workflow must not stop every other trigger on the
    heartbeat — and must not fail silently either.
    """
    current = now or datetime.now(timezone.utc)
    prune_fired(state, current)
    events: list[JiggaEvent] = []

    for workflow in sorted(workflows.values(), key=lambda w: w.id):
        trigger = workflow.trigger or {}
        name = trigger.get("event")
        if not name:
            continue
        evaluator = EVENT_EVALUATORS.get(str(name))
        if evaluator is None:
            events.append(JiggaEvent.create(
                "workflow.trigger_error", "scheduler", targets=[workflow.id],
                workflow=workflow.id, trigger=name,
                error=f"unknown event trigger {name!r} "
                      f"(known: {', '.join(sorted(EVENT_EVALUATORS)) or 'none'})"))
            continue
        try:
            subjects = evaluator(paths, workflow, trigger, current)
        except Exception as exc:  # noqa: BLE001 — one bad trigger must not stop the rest
            events.append(JiggaEvent.create(
                "workflow.trigger_error", "scheduler", targets=[workflow.id],
                workflow=workflow.id, trigger=name, error=str(exc)))
            continue
        for subject in subjects:
            key = fired_key(workflow.id, str(name), subject["id"])
            if already_fired(state, key):
                continue
            record_fire(state, key, current)
            events.append(JiggaEvent.create(
                "workflow.event_due", "scheduler", targets=[workflow.id],
                workflow=workflow.id, trigger=name, subject=subject["id"],
                trigger_payload=subject["payload"]))
    return events

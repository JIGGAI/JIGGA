from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, write_yaml
from jigga.core.models import now_iso
from jigga.runtime.audit import new_id

SESSION_GAP = timedelta(minutes=5)
CANDIDATE_EVENT_TYPES = frozenset({"agent.task_completed", "workflow.completed"})


def _read_events(logs_dir: Path) -> list[dict[str, Any]]:
    path = logs_dir / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug[:48] or "inferred_workflow"


def _event_key(event: dict[str, Any]) -> tuple[str, str]:
    details = event.get("details", {}) or {}
    agent_id = str(details.get("agent_id") or details.get("workflow") or "supervisor")
    title = str(details.get("title") or details.get("task_title") or event.get("type"))
    return agent_id, title


def _parse_time(event: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(event.get("time", "")))
    except ValueError:
        return None


def _group_sessions(events: list[dict[str, Any]], gap: timedelta = SESSION_GAP) -> list[list[dict[str, Any]]]:
    timed = [(e, _parse_time(e)) for e in events]
    timed.sort(key=lambda pair: (pair[1] or datetime.min, events.index(pair[0])))
    sessions: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    last_time: datetime | None = None
    for event, t in timed:
        if t is None:
            continue
        if last_time is not None and (t - last_time) > gap:
            if current:
                sessions.append(current)
            current = []
        current.append(event)
        last_time = t
    if current:
        sessions.append(current)
    return sessions


def _collapse_runs(session: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse consecutive identical (agent, title) events into one."""
    collapsed: list[dict[str, Any]] = []
    for event in session:
        if not collapsed or _event_key(collapsed[-1]) != _event_key(event):
            collapsed.append(event)
    return collapsed


def _modal_hour(sessions: list[list[dict[str, Any]]]) -> int | None:
    hours: list[int] = []
    for sess in sessions:
        t = _parse_time(sess[0])
        if t is not None:
            hours.append(t.hour)
    if not hours:
        return None
    top_hour, top_count = Counter(hours).most_common(1)[0]
    # Only report the hour when a clear majority of sessions cluster there.
    if top_count >= max(2, (len(sessions) // 2) + 1):
        return top_hour
    return None


def _build_step(index: int, agent_id: str, title: str, orchestrator: str | None) -> dict[str, Any]:
    """A RUNNABLE step: the orchestrator (chief/default agent) re-dispatches the
    observed task to the agent that did it, via the bundled `task.assign` action.
    Running the workflow re-runs the recurring sequence. `orchestrator=None`
    leaves the executor unset (the plan flags it until a default agent exists)."""
    assignee = "supervisor" if agent_id == "supervisor" else agent_id
    # No hardcoded `approval: required` — the step is a real `task.assign` action
    # governed by the normal capability policy (medium risk → needs approval
    # unless the orchestrator is autonomous), so an autonomous chief can run the
    # workflow end-to-end while others still get the gate.
    return {
        "id": f"step_{index}",
        "agent": orchestrator,
        "action": "task.assign",
        "input": {"assignee": assignee, "title": title},
    }


def _format_label(shape: tuple[tuple[str, str], ...]) -> str:
    if len(shape) == 1:
        return shape[0][1]
    return f"{shape[0][1]} → … ({len(shape)} steps)"


def _build_suggestion(
    shape: tuple[tuple[str, str], ...],
    count: int,
    source_event_id: str | None,
    modal_hour: int | None,
    orchestrator: str | None = None,
) -> dict[str, Any]:
    label = _format_label(shape)
    first_agent = shape[0][0]
    workflow_id = f"suggested_{_slug(first_agent)}_{_slug(label)}"
    steps = [_build_step(idx, agent_id, title, orchestrator)
             for idx, (agent_id, title) in enumerate(shape, start=1)]
    purpose = (
        f"Inferred from {count} repeated occurrence(s) covering {len(shape)} step(s)."
    )
    suggestion: dict[str, Any] = {
        "id": workflow_id,
        "name": f"Suggested: {label}",
        "purpose": purpose,
        "status": "suggested",
        "confidence": min(0.95, 0.5 + count * 0.1),
        "evidence_count": count,
        "step_count": len(shape),
        "modal_hour_utc": modal_hour,
        "source_event_id": source_event_id,
        "workflow": {
            "id": workflow_id,
            "name": f"Suggested: {label}",
            "purpose": purpose,
            "status": "suggested",
            "trigger": {"type": "manual"},
            "steps": steps,
            "outputs": ["review_suggested_result"],
        },
    }
    if modal_hour is not None:
        suggestion["hint"] = (
            f"Sessions cluster around hour {modal_hour:02d}:00 UTC; "
            "consider a scheduled trigger after manual review."
        )
    return suggestion


def suggest_workflows(
    logs_dir: Path,
    min_count: int = 2,
    session_gap_minutes: int = 5,
    orchestrator: str | None = None,
) -> list[dict[str, Any]]:
    events = _read_events(logs_dir)
    candidates = [event for event in events if event.get("type") in CANDIDATE_EVENT_TYPES]

    # Signal A — multi-step session shapes (the new signal). Group into time
    # windows, collapse runs of identical events inside a window, then count
    # shapes of length >= 2.
    raw_sessions = _group_sessions(candidates, gap=timedelta(minutes=session_gap_minutes))
    collapsed_sessions = [_collapse_runs(sess) for sess in raw_sessions if sess]
    multi_shape_to_sessions: dict[tuple[tuple[str, str], ...], list[list[dict[str, Any]]]] = {}
    for sess in collapsed_sessions:
        if len(sess) > 1:
            shape = tuple(_event_key(event) for event in sess)
            multi_shape_to_sessions.setdefault(shape, []).append(sess)

    # Signal B — single-action repetition (the legacy signal). Count distinct
    # event occurrences regardless of session boundaries; suggests a one-step
    # workflow when the same action recurs.
    single_counter: Counter[tuple[str, str]] = Counter()
    single_examples: dict[tuple[str, str], dict[str, Any]] = {}
    single_times: dict[tuple[str, str], list[datetime]] = {}
    for event in candidates:
        key = _event_key(event)
        single_counter[key] += 1
        single_examples.setdefault(key, event)
        t = _parse_time(event)
        if t is not None:
            single_times.setdefault(key, []).append(t)

    suppressed_keys: set[tuple[str, str]] = set()
    suggestions: list[dict[str, Any]] = []

    # Multi-step suggestions take precedence — they describe richer behavior.
    for shape, sess_list in sorted(
        multi_shape_to_sessions.items(), key=lambda kv: (-len(kv[1]), kv[0])
    ):
        count = len(sess_list)
        if count < min_count:
            continue
        modal_hour = _modal_hour(sess_list)
        suggestions.append(
            _build_suggestion(
                shape=shape,
                count=count,
                source_event_id=sess_list[0][0].get("id"),
                modal_hour=modal_hour,
                orchestrator=orchestrator,
            )
        )
        suppressed_keys.update(shape)

    # Single-step suggestions for actions not already covered by a multi-step shape.
    for (agent_id, title), count in single_counter.most_common():
        if count < min_count:
            continue
        if (agent_id, title) in suppressed_keys:
            continue
        times = single_times.get((agent_id, title), [])
        wrapped = [[{"time": t.isoformat(), "details": {}}] for t in times]
        modal_hour = _modal_hour(wrapped) if wrapped else None
        suggestions.append(
            _build_suggestion(
                shape=((agent_id, title),),
                count=count,
                source_event_id=single_examples[(agent_id, title)].get("id"),
                modal_hour=modal_hour,
                orchestrator=orchestrator,
            )
        )
    return suggestions


def write_suggestion(workflows_dir: Path, suggestion: dict[str, Any]) -> Path:
    ensure_dir(workflows_dir)
    target = workflows_dir / f"{suggestion['id']}.yaml"
    if target.exists():
        raise FileExistsError(f"Workflow already exists: {target}")
    write_yaml(target, suggestion["workflow"])
    return target


def apply_suggestion(
    workflows_dir: Path,
    suggestion_id: str,
    logs_dir: Path,
    approve: bool = False,
    orchestrator: str | None = None,
) -> dict[str, Any]:
    suggestions = {suggestion["id"]: suggestion
                   for suggestion in suggest_workflows(logs_dir, orchestrator=orchestrator)}
    suggestion = suggestions.get(suggestion_id)
    if suggestion is None:
        return {"status": "not_found", "suggestion_id": suggestion_id}
    if not approve:
        return {"status": "needs_approval", "suggestion": suggestion}
    target = workflows_dir / f"{suggestion['id']}.yaml"
    if target.exists():
        return {
            "status": "already_applied",
            "path": str(target),
            "suggestion": suggestion,
        }
    written = write_suggestion(workflows_dir, suggestion)
    return {"status": "applied", "path": str(written), "applied_at": now_iso(), "suggestion": suggestion}


def record_manual_pattern(logs_dir: Path, agent_id: str, title: str) -> dict[str, Any]:
    ensure_dir(logs_dir)
    event = {
        "id": new_id("evt"),
        "time": now_iso(),
        "type": "agent.task_completed",
        "status": "ok",
        "details": {"agent_id": agent_id, "title": title},
    }
    with (logs_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return event

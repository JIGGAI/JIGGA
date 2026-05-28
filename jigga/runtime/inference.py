from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, write_yaml
from jigga.runtime.audit import new_id
from jigga.core.models import now_iso


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


def suggest_workflows(logs_dir: Path, min_count: int = 2) -> list[dict[str, Any]]:
    events = _read_events(logs_dir)
    candidates = [event for event in events if event.get("type") in {"agent.task_completed", "workflow.completed"}]
    counter: Counter[tuple[str, str]] = Counter()
    examples: dict[tuple[str, str], dict[str, Any]] = {}
    for event in candidates:
        details = event.get("details", {})
        agent_id = str(details.get("agent_id") or details.get("workflow") or "supervisor")
        title = str(details.get("title") or details.get("task_title") or event.get("type"))
        key = (agent_id, title)
        counter[key] += 1
        examples.setdefault(key, event)

    suggestions: list[dict[str, Any]] = []
    for (agent_id, title), count in counter.most_common():
        if count < min_count:
            continue
        workflow_id = f"suggested_{_slug(agent_id)}_{_slug(title)}"
        suggestions.append(
            {
                "id": workflow_id,
                "name": f"Suggested: {title}",
                "purpose": f"Inferred from {count} repeated audit events.",
                "status": "suggested",
                "confidence": min(0.95, 0.5 + count * 0.1),
                "evidence_count": count,
                "source_event_id": examples[(agent_id, title)].get("id"),
                "workflow": {
                    "id": workflow_id,
                    "name": f"Suggested: {title}",
                    "purpose": f"Inferred from {count} repeated audit events.",
                    "status": "suggested",
                    "trigger": {"type": "manual"},
                    "steps": [
                        {
                            "id": "run_inferred_task",
                            "agent": agent_id if agent_id != "supervisor" else None,
                            "action": title,
                            "approval": "required",
                        }
                    ],
                    "outputs": ["review_suggested_result"],
                },
            }
        )
    return suggestions


def write_suggestion(workflows_dir: Path, suggestion: dict[str, Any]) -> Path:
    ensure_dir(workflows_dir)
    target = workflows_dir / f"{suggestion['id']}.yaml"
    if target.exists():
        raise FileExistsError(f"Workflow already exists: {target}")
    write_yaml(target, suggestion["workflow"])
    return target


def apply_suggestion(workflows_dir: Path, suggestion_id: str, logs_dir: Path, approve: bool = False) -> dict[str, Any]:
    suggestions = {suggestion["id"]: suggestion for suggestion in suggest_workflows(logs_dir)}
    suggestion = suggestions.get(suggestion_id)
    if suggestion is None:
        return {"status": "not_found", "suggestion_id": suggestion_id}
    if not approve:
        return {"status": "needs_approval", "suggestion": suggestion}
    target = write_suggestion(workflows_dir, suggestion)
    return {"status": "applied", "path": str(target), "applied_at": now_iso(), "suggestion": suggestion}


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

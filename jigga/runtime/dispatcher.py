from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, write_json
from jigga.core.models import WorkflowStep
from jigga.runtime.audit import append_event
from jigga.runtime.capabilities import CapabilityManifest, CapabilityRegistry


def resolve_value(value: Any, outputs: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return outputs.get(value, value)
    if isinstance(value, list):
        return [resolve_value(item, outputs) for item in value]
    if isinstance(value, dict):
        return {key: resolve_value(item, outputs) for key, item in value.items()}
    return value


def _dry_run_output(step: WorkflowStep, capability: CapabilityManifest, resolved_input: Any, context: dict[str, Any]) -> Any:
    if step.action == "calendar.list_events":
        return [
            {"time": "09:30", "title": "Planning block", "source": "capability.dry_run"},
            {"time": "14:00", "title": "Project review", "source": "capability.dry_run"},
        ]
    if step.action == "calendar.get_event":
        return {"title": "Project review", "time": "14:00", "source": "capability.dry_run"}
    if step.action == "email.search":
        return [{"from": "client@example.com", "subject": "Launch follow-up", "source": "capability.dry_run"}]
    if step.action in {"summarize_day", "summarize_relevant_context"}:
        return {"summary": f"MVP summary for {step.id}", "input": resolved_input, "memory_context": context}
    if step.action == "notifications.send":
        return {"dry_run": True, "delivered": False, "input": resolved_input}
    return {
        "dry_run": True,
        "capability": capability.name,
        "action": step.action,
        "input": resolved_input,
    }


def execute_step(
    step: WorkflowStep,
    run_dir: Path,
    outputs: dict[str, Any],
    context: dict[str, Any],
    registry: CapabilityRegistry,
    logs_dir: Path,
    workflow_id: str,
    run_id: str,
) -> tuple[Any, Path | None]:
    ensure_dir(run_dir)
    capability = registry.resolve_action(step.action)
    if capability is None:
        raise ValueError(f"No capability registered for workflow action: {step.action}")

    resolved_input = resolve_value(step.input, outputs)
    append_event(
        logs_dir,
        "capability.invocation.started",
        workflow=workflow_id,
        run_id=run_id,
        step=step.id,
        action=step.action,
        capability=capability.name,
        risk_level=capability.risk_level,
    )
    output = _dry_run_output(step, capability, resolved_input, context)

    artifact = None
    if step.output:
        artifact = run_dir / step.output
        if artifact.suffix in {".md", ".txt"}:
            artifact.write_text(str(output), encoding="utf-8")
        else:
            write_json(artifact, output)

    append_event(
        logs_dir,
        "capability.invocation.completed",
        workflow=workflow_id,
        run_id=run_id,
        step=step.id,
        action=step.action,
        capability=capability.name,
        artifact=str(artifact) if artifact else None,
    )
    return output, artifact

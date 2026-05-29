from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from jigga.core.io import ensure_dir, write_json
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.core.paths import resolve_home
from jigga.runtime.audit import append_event
from jigga.runtime.capabilities import CapabilityManifest, CapabilityRegistry
from jigga.runtime.policy import PolicyDecision, evaluate_filesystem
from jigga.runtime.subagents import spawn_subagent


def resolve_value(value: Any, outputs: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return outputs.get(value, value)
    if isinstance(value, list):
        return [resolve_value(item, outputs) for item in value]
    if isinstance(value, dict):
        return {key: resolve_value(item, outputs) for key, item in value.items()}
    return value


def evaluate_capability_permissions(capability: CapabilityManifest, agent: AgentConfig | None) -> PolicyDecision:
    if agent is None:
        return PolicyDecision("deny", "No agent configured for capability permission check.", "agent.available")
    filesystem = capability.permissions.get("filesystem") if isinstance(capability.permissions, dict) else None
    if isinstance(filesystem, dict):
        for operation in ("read", "write"):
            for path in list(filesystem.get(operation, []) or []):
                decision = evaluate_filesystem(agent, path, operation=operation)
                if decision.status != "allow":
                    return decision
    return PolicyDecision("allow")


def _calendar_handler(step: WorkflowStep, _capability: CapabilityManifest, resolved_input: Any, _context: dict[str, Any]) -> Any:
    if step.action == "calendar.list_events":
        return [
            {"time": "09:30", "title": "Planning block", "source": "capability.dry_run"},
            {"time": "14:00", "title": "Project review", "source": "capability.dry_run"},
        ]
    if step.action == "calendar.get_event":
        return {"title": "Project review", "time": "14:00", "source": "capability.dry_run", "input": resolved_input}
    return _generic_handler(step, _capability, resolved_input, _context)


def _email_handler(step: WorkflowStep, _capability: CapabilityManifest, resolved_input: Any, _context: dict[str, Any]) -> Any:
    return [{"from": "client@example.com", "subject": "Launch follow-up", "source": "capability.dry_run", "input": resolved_input}]


def _notifications_handler(step: WorkflowStep, _capability: CapabilityManifest, resolved_input: Any, _context: dict[str, Any]) -> Any:
    return {"dry_run": True, "delivered": False, "source": "capability.dry_run", "input": resolved_input}


def _summarization_handler(step: WorkflowStep, _capability: CapabilityManifest, resolved_input: Any, context: dict[str, Any]) -> Any:
    memory_context = {key: value for key, value in context.items() if key not in {"agent", "home", "logs_dir", "sessions_dir"}}
    return {"summary": f"MVP summary for {step.id}", "source": "capability.dry_run", "input": resolved_input, "memory_context": memory_context}


def _spawn_subagent_handler(step: WorkflowStep, capability: CapabilityManifest, resolved_input: Any, context: dict[str, Any]) -> Any:
    agent = context.get("agent")
    if not isinstance(agent, AgentConfig):
        raise ValueError("spawn_subagent requires an executing agent")
    home = Path(context.get("home") or resolve_home(None))
    logs_dir = Path(context.get("logs_dir") or home / "logs")
    sessions_dir = Path(context.get("sessions_dir") or home / "sessions")
    payload = resolved_input if isinstance(resolved_input, dict) else {}
    session = spawn_subagent(home, logs_dir, sessions_dir, agent, payload)
    return session.to_dict()


def _generic_handler(step: WorkflowStep, capability: CapabilityManifest, resolved_input: Any, _context: dict[str, Any]) -> Any:
    return {
        "dry_run": True,
        "source": "capability.dry_run",
        "capability": capability.name,
        "action": step.action,
        "input": resolved_input,
    }


Handler = Callable[[WorkflowStep, CapabilityManifest, Any, dict[str, Any]], Any]
HANDLERS: dict[str, Handler] = {
    "dry_run.calendar": _calendar_handler,
    "dry_run.email": _email_handler,
    "dry_run.notifications": _notifications_handler,
    "dry_run.summarization": _summarization_handler,
    "dry_run.generic": _generic_handler,
    "runtime.spawn_subagent": _spawn_subagent_handler,
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
        handler=capability.handler,
    )
    handler = HANDLERS.get(capability.handler)
    if handler is None:
        raise ValueError(f"No handler registered for capability {capability.name}: {capability.handler}")
    output = handler(step, capability, resolved_input, context)

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
        handler=capability.handler,
        artifact=str(artifact) if artifact else None,
    )
    return output, artifact

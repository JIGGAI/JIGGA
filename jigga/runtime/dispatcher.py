from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jigga.core.io import ensure_dir, write_json
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.audit import append_event
from jigga.runtime.capabilities import CapabilityManifest, CapabilityRegistry
from jigga.runtime.policy import PolicyDecision, evaluate_filesystem
from jigga.runtime.subagents import spawn_subagent


@dataclass(frozen=True)
class RuntimeContext:
    """Runtime plumbing passed to capability handlers, kept separate from
    `memory_context` so memory-derived output never accidentally captures
    paths or runtime references."""

    agent: AgentConfig | None
    home: Path
    logs_dir: Path
    sessions_dir: Path


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


def _calendar_handler(
    step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    _runtime: RuntimeContext,
) -> Any:
    if step.action == "calendar.list_events":
        return [
            {"time": "09:30", "title": "Planning block", "source": "capability.dry_run"},
            {"time": "14:00", "title": "Project review", "source": "capability.dry_run"},
        ]
    if step.action == "calendar.get_event":
        return {"title": "Project review", "time": "14:00", "source": "capability.dry_run", "input": resolved_input}
    return _generic_handler(step, _capability, resolved_input, _memory_context, _runtime)


def _email_handler(
    _step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    _runtime: RuntimeContext,
) -> Any:
    return [{"from": "client@example.com", "subject": "Launch follow-up", "source": "capability.dry_run", "input": resolved_input}]


def _notifications_handler(
    _step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    _runtime: RuntimeContext,
) -> Any:
    return {"dry_run": True, "delivered": False, "source": "capability.dry_run", "input": resolved_input}


def _summarization_handler(
    step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    memory_context: dict[str, Any],
    _runtime: RuntimeContext,
) -> Any:
    return {
        "summary": f"MVP summary for {step.id}",
        "source": "capability.dry_run",
        "input": resolved_input,
        "memory_context": memory_context,
    }


def _spawn_subagent_handler(
    _step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime: RuntimeContext,
) -> Any:
    if runtime.agent is None:
        raise ValueError("spawn_subagent requires an executing agent")
    payload = resolved_input if isinstance(resolved_input, dict) else {}
    session = spawn_subagent(runtime.home, runtime.logs_dir, runtime.sessions_dir, runtime.agent, payload)
    return session.to_dict()


def _generic_handler(
    step: WorkflowStep,
    capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    _runtime: RuntimeContext,
) -> Any:
    return {
        "dry_run": True,
        "source": "capability.dry_run",
        "capability": capability.name,
        "action": step.action,
        "input": resolved_input,
    }


Handler = Callable[
    [WorkflowStep, CapabilityManifest, Any, dict[str, Any], RuntimeContext],
    Any,
]
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
    memory_context: dict[str, Any],
    runtime: RuntimeContext,
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
    output = handler(step, capability, resolved_input, memory_context, runtime)

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

from __future__ import annotations

import importlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from jigga.core.io import ensure_dir, write_json
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.audit import append_event
from jigga.runtime.capabilities import CapabilityManifest, CapabilityRegistry
from jigga.runtime.policy import PolicyDecision, evaluate_filesystem, evaluate_network, evaluate_resource_permission

# Capabilities declare flat scalar permissions like `{calendar: "read"}` or
# `{notifications: "send"}`. These are dispatched to evaluate_resource_permission.
# Filesystem and network use their own structured evaluators. Memory is handled
# separately via memory_scope. Delegation is enforced inside spawn_subagent.
SCALAR_CAPABILITY_RESOURCES = ("calendar", "email", "notifications")
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
    permissions = capability.permissions if isinstance(capability.permissions, dict) else {}

    # Filesystem — structured allow/deny lists per operation.
    filesystem = permissions.get("filesystem")
    if isinstance(filesystem, dict):
        for operation in ("read", "write"):
            for path in list(filesystem.get(operation, []) or []):
                decision = evaluate_filesystem(agent, path, operation=operation)
                if decision.status != "allow":
                    return decision

    # Network — mode-based; if the capability declares any network usage we
    # require the agent's network mode to permit it. Supports either
    # `{network: "allow"}` (flat scalar grant) or `{network: {mode: "allow"}}`
    # / `{network: {target: "..."}}` shapes.
    network = permissions.get("network")
    if network is not None:
        target = network.get("target") if isinstance(network, dict) else None
        decision = evaluate_network(agent, str(target) if target else None)
        if decision.status != "allow":
            return decision

    # Memory — governed by the memory_scope mechanism, not a flat permission
    # value. Capabilities declaring memory access require the agent to have a
    # memory_scope assigned; the scope itself controls what's visible.
    if permissions.get("memory") is not None and not agent.memory_scope:
        return PolicyDecision(
            "deny",
            f"Capability {capability.name} needs memory access but agent {agent.id} has no memory_scope.",
            "memory.scope",
        )

    # Flat scalar resources: calendar/email/notifications.
    for resource in SCALAR_CAPABILITY_RESOURCES:
        required = permissions.get(resource)
        if required is None:
            continue
        # If the capability declares a structured shape, take the operation
        # from the relevant key; otherwise the value itself is the operation.
        operation = required if isinstance(required, str) else str(required.get("operation") or "")
        if not operation:
            continue
        decision = evaluate_resource_permission(agent, resource, operation)
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


@lru_cache(maxsize=64)
def _import_handler(path: str) -> Handler:
    """Resolve a `module.path:function` style handler reference.

    User-local capability manifests can declare a dotted import path so they
    don't have to register inside `HANDLERS`. Built-in handlers continue to use
    the short string keys for backwards-compat and to avoid making the dispatch
    surface dependent on package layout. Cached to avoid repeat import cost.

    Trust boundary: the import target is fully under the user's control via
    the manifest. First-use approval for user-local packs (see capability
    approvals mechanism) is what gates trust. The runtime does not validate
    that the imported callable is safe.
    """
    if ":" not in path:
        raise ValueError(
            f"Handler {path!r} must be either a built-in key in HANDLERS or a "
            "'module.path:function' import reference."
        )
    module_name, _, function_name = path.partition(":")
    if not module_name or not function_name:
        raise ValueError(f"Invalid handler import reference: {path!r}")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(f"Cannot import handler module {module_name!r}: {exc}") from exc
    handler = getattr(module, function_name, None)
    if not callable(handler):
        raise ValueError(f"Handler {path!r} resolved to non-callable: {type(handler).__name__}")
    return handler


def resolve_handler(name: str) -> Handler:
    handler = HANDLERS.get(name)
    if handler is not None:
        return handler
    return _import_handler(name)


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
    try:
        handler = resolve_handler(capability.handler)
    except ValueError as exc:
        raise ValueError(
            f"No handler registered for capability {capability.name}: {capability.handler} ({exc})"
        ) from exc
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

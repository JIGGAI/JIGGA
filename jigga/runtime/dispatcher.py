from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from jigga.core.io import ensure_dir, write_json
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.audit import append_event
from jigga.runtime.capabilities import CapabilityManifest, CapabilityRegistry
from jigga.runtime.mcp_client import call_mcp_tool
from jigga.runtime.notifications import (
    NotificationRequest,
    delivery_mode,
    send_notification,
)
from jigga.runtime.model_router import (
    ModelCallItem,
    ModelCallRequest,
    call_model,
    resolve_agent_model,
    resolve_agent_model_profile,
)
from jigga.runtime.policy import (
    PolicyDecision,
    evaluate_filesystem,
    evaluate_network,
    evaluate_resource_permission,
)
from jigga.runtime.sandbox import SandboxSpec, build_restricted_env
from jigga.runtime.subagents import spawn_subagent

# Capabilities declare flat scalar permissions like `{calendar: "read"}` or
# `{notifications: "send"}`. These are dispatched to evaluate_resource_permission.
# Filesystem and network use their own structured evaluators. Memory is handled
# separately via memory_scope. Delegation is enforced inside spawn_subagent.
SCALAR_CAPABILITY_RESOURCES = ("calendar", "email", "notifications")


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


def _requests_resource_access(declared: Any) -> bool:
    """Return True when a capability's resource declaration *requests* access.

    Self-restricting declarations like `{network: {mode: deny}}` don't ask the
    agent for network and shouldn't force the agent's policy to permit it.
    """
    if isinstance(declared, str):
        return declared.lower() not in {"deny", "disabled", "none", "off"}
    if isinstance(declared, dict):
        mode = str(declared.get("mode", "")).lower()
        return mode not in {"deny", "disabled", "none", "off"}
    return True


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

    # Network — only gated when the capability *requests* network access.
    # A capability declaring `{network: {mode: deny}}` is self-restricting
    # (saying "I won't touch the network") — that's information, not a
    # request, and shouldn't force the agent's network mode to be open.
    network = permissions.get("network")
    if network is not None and _requests_resource_access(network):
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


def _notifications_dry_run_handler(
    _step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    _runtime: RuntimeContext,
) -> Any:
    return {"dry_run": True, "delivered": False, "source": "capability.dry_run", "input": resolved_input}


def _coerce_notification_body(value: Any) -> str:
    """Render whatever a workflow upstream produced into a single string suitable
    for `display notification` / `notify-send`. Prefer a `summary` key when the
    upstream output is a dict (matches the shape `summarize_day` produces),
    fall back to JSON for other dicts, stringify scalars."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("summary"), str):
            return value["summary"]
        if isinstance(value.get("content"), str):
            return value["content"]
        return json.dumps(value, indent=2)
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value)


def _notifications_handler(
    _step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime: RuntimeContext,
) -> Any:
    """Real notification delivery via `runtime.notifications`.

    Accepts either a structured `{title, body|content, urgency}` input from
    the workflow step, or a bare scalar that becomes the body. Looks up the
    runtime's delivery_mode (real / dry_run) and routes accordingly. Every
    invocation emits a `notification.delivered` or `notification.failed`
    audit event so the user can tell whether the desktop saw it.
    """
    if isinstance(resolved_input, dict):
        title = str(resolved_input.get("title") or "JIGGA")
        body = _coerce_notification_body(
            resolved_input.get("body") if resolved_input.get("body") is not None else resolved_input.get("content")
        )
        urgency = str(resolved_input.get("urgency") or "normal").lower()
    else:
        title = "JIGGA"
        body = _coerce_notification_body(resolved_input)
        urgency = "normal"

    mode = delivery_mode(runtime.home)
    is_dry_run = mode == "dry_run"
    request = NotificationRequest(title=title, body=body, urgency=urgency)
    result = send_notification(request, dry_run=is_dry_run)
    append_event(
        runtime.logs_dir,
        "notification.delivered" if result.delivered else "notification.failed",
        status="ok" if result.delivered else "error",
        backend=result.backend,
        dry_run=is_dry_run,
        urgency=urgency,
        title=title,
        error=result.error,
    )
    return {
        "source": "capability.notifications",
        "delivered": result.delivered,
        "backend": result.backend,
        "title": title,
        "body": body,
        "urgency": urgency,
        "dry_run": is_dry_run,
        "error": result.error,
    }


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


def _skill_pack_handler(
    step: WorkflowStep,
    capability: CapabilityManifest,
    resolved_input: Any,
    memory_context: dict[str, Any],
    runtime: RuntimeContext,
) -> Any:
    """Dispatch a skill_pack capability.

    Loads the capability's `instructions` file from the pack directory, puts it
    in the system role of a model call, and feeds the resolved step input as
    the user role. The memory_context rides along so a scoped agent can ground
    the response in its memory.
    """
    if not capability.source or capability.source == "builtin":
        raise ValueError(
            f"skill_pack capability {capability.name!r} requires a file-backed source "
            "(bundled skill packs are not supported)."
        )
    pack_dir = Path(capability.source).parent
    instructions_path = pack_dir / capability.instructions
    if not instructions_path.exists():
        raise ValueError(
            f"skill_pack {capability.name!r} missing instructions at {instructions_path}"
        )
    instructions = instructions_path.read_text(encoding="utf-8")

    if runtime.agent is None:
        raise ValueError("skill_pack invocation requires an executing agent")

    rendered_input = (
        json.dumps(resolved_input, indent=2)
        if not isinstance(resolved_input, str)
        else resolved_input
    )
    task = {"id": f"skill_{step.id}", "title": step.action, "description": rendered_input}
    items = [
        ModelCallItem(
            id="skill_instructions",
            role="system",
            content=f"You are the {capability.name!r} skill.\n\n{instructions}",
        ),
        ModelCallItem(
            id=f"step:{step.id}",
            role="user",
            content=f"Action: {step.action}\nInput:\n{rendered_input}",
        ),
    ]
    request = ModelCallRequest(
        agent_id=runtime.agent.id,
        role=runtime.agent.role,
        task=task,
        items=items,
        model=resolve_agent_model(runtime.agent),
        model_profile=resolve_agent_model_profile(runtime.agent),
        dry_run=False,
    )
    result = call_model(runtime.home, runtime.logs_dir, request)
    return {
        "source": "capability.skill_pack",
        "skill": capability.name,
        "action": step.action,
        "model_provider": result.provider,
        "model": result.model,
        "content": result.content,
        "memory_context": memory_context,
    }


def _capability_secrets_required(capability: CapabilityManifest) -> list[str]:
    secrets = (
        capability.permissions.get("secrets")
        if isinstance(capability.permissions, dict)
        else None
    )
    if isinstance(secrets, dict):
        return [str(item) for item in (secrets.get("required") or [])]
    return []


def _mcp_restricted_env(capability: CapabilityManifest) -> dict[str, str]:
    """Thin wrapper that delegates to runtime.sandbox. Kept for backwards-compat
    in case callers want the env dict without spawning a process."""
    return build_restricted_env(_capability_secrets_required(capability))


def _mcp_server_handler(
    step: WorkflowStep,
    capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime: RuntimeContext,
) -> Any:
    """Dispatch an mcp_server capability.

    Spawns the declared command + args as a subprocess and exchanges JSON-RPC
    over stdio per the MCP spec. The subprocess runs in the pack directory so
    relative args (e.g. `server.py`) resolve correctly; env is restricted to
    PATH/HOME/etc. plus any secrets the manifest explicitly requested.
    """
    if not capability.command:
        raise ValueError(
            f"mcp_server capability {capability.name!r} has no command declared"
        )
    if runtime.agent is None:
        raise ValueError("mcp_server invocation requires an executing agent")

    if capability.source and capability.source != "builtin":
        cwd: Path = Path(capability.source).parent
    else:
        cwd = runtime.home

    arguments = (
        resolved_input
        if isinstance(resolved_input, dict)
        else {"input": resolved_input}
    )
    spec = SandboxSpec(
        command=capability.command,
        args=list(capability.args),
        cwd=cwd,
        secrets_required=_capability_secrets_required(capability),
        timeout_seconds=float(
            capability.requires.get("timeout_seconds", 30)
            if isinstance(capability.requires, dict)
            else 30
        ),
    )
    result = call_mcp_tool(spec, tool_name=step.action, arguments=arguments)
    return {
        "source": "capability.mcp_server",
        "capability": capability.name,
        "action": step.action,
        "result": result,
    }


Handler = Callable[
    [WorkflowStep, CapabilityManifest, Any, dict[str, Any], RuntimeContext],
    Any,
]
HANDLERS: dict[str, Handler] = {
    "dry_run.calendar": _calendar_handler,
    "dry_run.email": _email_handler,
    "dry_run.notifications": _notifications_dry_run_handler,
    "runtime.notifications": _notifications_handler,
    "dry_run.summarization": _summarization_handler,
    "dry_run.generic": _generic_handler,
    "runtime.spawn_subagent": _spawn_subagent_handler,
    "skill_pack.default": _skill_pack_handler,
    "mcp_server.subprocess": _mcp_server_handler,
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

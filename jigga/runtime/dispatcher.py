from __future__ import annotations

import importlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, write_json
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.audit import append_event
from jigga.runtime.capabilities import CapabilityManifest, CapabilityRegistry
from jigga.runtime.email_imap import email_imap_handler
from jigga.runtime.filesystem import filesystem_handler
from jigga.runtime.gog import gog_handler
from jigga.runtime.google_calendar import google_calendar_handler
from jigga.runtime.handlers import (
    _calendar_handler,
    _draft_prompt,
    _draft_with_model_handler,
    _email_handler,
    _generic_handler,
    _mcp_server_handler,
    _notifications_dry_run_handler,
    _mailbox_handler,
    _notifications_handler,
    _remember_handler,
    _search_memory_handler,
    _skill_pack_handler,
    _spawn_subagent_handler,
    _summarization_handler,
    _team_insight_handler,
    _team_orchestration_handler,
    _tickets_handler,
)
from jigga.runtime.policy import (
    PolicyDecision,
    evaluate_filesystem,
    evaluate_network,
    evaluate_resource_permission,
)
from jigga.runtime.reminders import reminders_handler
from jigga.runtime.runtime_context import Handler, RuntimeContext
from jigga.runtime.shell import shell_handler
from jigga.runtime.telegram import telegram_handler
from jigga.runtime.web import web_handler
from jigga.runtime.webchat import webchat_handler

# Re-exported for back-compat: callers historically import RuntimeContext and
# the handler functions from this module. Their canonical homes are now
# runtime_context and handlers; these keep existing import paths working.
__all__ = [
    "HANDLERS", "Handler", "RuntimeContext", "dispatch_action", "execute_step",
    "evaluate_capability_permissions", "resolve_handler", "resolve_value",
    "_draft_prompt", "_draft_with_model_handler", "_remember_handler",
    "_search_memory_handler",
]

# Capabilities declare flat scalar permissions like `{calendar: "read"}` or
# `{notifications: "send"}`. These are dispatched to evaluate_resource_permission.
# Filesystem and network use their own structured evaluators. Memory is handled
# separately via memory_scope. Secrets are handled explicitly because their
# manifest shape is `{required: [...]}`. Delegation is enforced inside spawn_subagent.
SCALAR_CAPABILITY_RESOURCES = ("calendar", "email", "notifications", "mailbox", "tickets")


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

    # Secrets — manifests declare concrete names under `required`; agents must
    # opt in with `permissions.secrets.allow` (or `mode: allow` for all). This
    # prevents an approved capability pack from implicitly receiving every
    # secret it names without an agent-level grant.
    secrets = permissions.get("secrets")
    if isinstance(secrets, dict):
        for secret_name in list(secrets.get("required") or []):
            decision = evaluate_resource_permission(agent, "secrets", str(secret_name))
            if decision.status != "allow":
                return decision

    return PolicyDecision("allow")


HANDLERS: dict[str, Handler] = {
    "dry_run.calendar": _calendar_handler,
    "dry_run.email": _email_handler,
    "dry_run.notifications": _notifications_dry_run_handler,
    "runtime.notifications": _notifications_handler,
    "runtime.mailbox": _mailbox_handler,
    "runtime.tickets": _tickets_handler,
    "dry_run.summarization": _summarization_handler,
    "dry_run.generic": _generic_handler,
    "runtime.spawn_subagent": _spawn_subagent_handler,
    "runtime.draft_with_model": _draft_with_model_handler,
    "runtime.search_memory": _search_memory_handler,
    "runtime.remember": _remember_handler,
    "runtime.email_imap": email_imap_handler,
    "runtime.filesystem": filesystem_handler,
    "runtime.google_calendar": google_calendar_handler,
    "runtime.gog": gog_handler,
    "runtime.shell": shell_handler,
    "runtime.reminders": reminders_handler,
    "runtime.telegram": telegram_handler,
    "runtime.web": web_handler,
    "runtime.webchat": webchat_handler,
    "skill_pack.default": _skill_pack_handler,
    "mcp_server.subprocess": _mcp_server_handler,
    "runtime.team_insight": _team_insight_handler,
    "runtime.team_orchestration": _team_orchestration_handler,
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


def dispatch_action(
    step: WorkflowStep,
    resolved_input: Any,
    memory_context: dict[str, Any],
    runtime: RuntimeContext,
    registry: CapabilityRegistry,
    logs_dir: Path,
    *,
    run_id: str,
    workflow_id: str | None = None,
) -> Any:
    """Resolve and invoke a single capability action — the one code path for
    *all* capability invocation, whether it comes from a workflow step or (in a
    later PR) an agent tool call.

    Takes a `WorkflowStep` because handlers read `step.action`/`step.id`; a
    non-workflow caller (agent loop) synthesizes a lightweight step. Emits the
    `capability.invocation.started/completed` audit events so every invocation
    is traced identically. Returns the handler's output; artifact writing stays
    with the workflow caller (it's workflow-run-dir specific).
    """
    capability = registry.resolve_action(step.action)
    if capability is None:
        raise ValueError(f"No capability registered for action: {step.action}")
    # Runtime-only actions (e.g. channel ingest) are never agent-callable —
    # defense in depth on top of the grant-time exclusion, covering installs
    # that granted them before the distinction existed.
    if capability.is_runtime_only(step.action) and runtime.agent is not None:
        append_event(logs_dir, "capability.invocation.denied", run_id=run_id, step=step.id,
                     action=step.action, capability=capability.name, status="deny",
                     reason="runtime-only action — the supervisor owns this; agents must not call it")
        raise ValueError(
            f"{step.action} is runtime-only (the supervisor owns it); agents cannot call it.")

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

    append_event(
        logs_dir,
        "capability.invocation.completed",
        workflow=workflow_id,
        run_id=run_id,
        step=step.id,
        action=step.action,
        capability=capability.name,
        handler=capability.handler,
    )
    return output


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
    resolved_input = resolve_value(step.input, outputs)
    output = dispatch_action(
        step,
        resolved_input,
        memory_context,
        runtime,
        registry,
        logs_dir,
        run_id=run_id,
        workflow_id=workflow_id,
    )

    artifact = None
    if step.output:
        artifact = run_dir / step.output
        if artifact.suffix in {".md", ".txt"}:
            artifact.write_text(str(output), encoding="utf-8")
        else:
            write_json(artifact, output)
    # NB: the artifact path is also recorded by run_workflow's own
    # `workflow.step.completed` event, so dropping it from the capability
    # invocation trace loses no information.
    return output, artifact

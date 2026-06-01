"""Capability handlers — the functions the dispatcher invokes per action.

Each handler has the signature `(step, capability, resolved_input,
memory_context, runtime) -> Any`. The registry that maps capability handler
keys to these functions, and the single `dispatch_action` invocation path, live
in `dispatcher.py`; this module is just the handler bodies so the dispatch spine
stays small. Memory/workspace deps are imported lazily inside the functions that
use them to avoid import cycles.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.audit import append_event
from jigga.runtime.capabilities import CapabilityManifest
from jigga.runtime.mcp_client import call_mcp_tool
from jigga.runtime.model_router import (
    ModelCallItem,
    ModelCallRequest,
    call_model,
    resolve_agent_model,
    resolve_agent_model_profile,
)
from jigga.runtime.notifications import (
    NotificationRequest,
    delivery_mode,
    send_notification,
)
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.sandbox import SandboxSpec, build_restricted_env
from jigga.runtime.subagents import spawn_subagent

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


def _agent_team_id(runtime: RuntimeContext) -> str | None:
    """The workspace id whose memory this agent should see — its (first) team, or
    its own id when team-less. Read-only (no scaffolding)."""
    from jigga.runtime.workspaces import find_agent_teams

    if runtime.agent is None:
        return None
    teams = find_agent_teams(runtime.home / "teams", runtime.agent.id)
    return teams[0].id if teams else runtime.agent.id


def _search_memory_handler(
    _step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime: RuntimeContext,
) -> Any:
    """Keyword search over the agent's memory (`memory.search`). Input is a query
    string, or `{query, scope?, limit?}`. With an explicit `scope`, searches that
    memory scope; otherwise searches global memory + the agent's own team memory."""
    from jigga.runtime.memory_index import search_memory

    if isinstance(resolved_input, dict):
        query = str(resolved_input.get("query") or resolved_input.get("q") or "")
        scope = resolved_input.get("scope")
        limit = int(resolved_input.get("limit") or 10)
    else:
        query, scope, limit = str(resolved_input or ""), None, 10
    team = None if scope else _agent_team_id(runtime)
    results = search_memory(runtime.home / "memory", query, scope=scope, team=team, limit=limit)
    return {"source": "capability.memory_search", "query": query, "scope": scope, "team": team, "results": results}


def _remember_handler(
    _step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime: RuntimeContext,
) -> Any:
    """Persist a durable fact to the agent's team memory (`memory.remember`).
    Input is text, or `{text, type?, tags?}`."""
    from jigga.runtime.team_memory import append_team_memory
    from jigga.runtime.workspaces import ensure_agent_workspace

    if runtime.agent is None:
        raise ValueError("memory.remember requires an executing agent")
    if isinstance(resolved_input, dict):
        text = str(resolved_input.get("text") or resolved_input.get("content") or "")
        mem_type = str(resolved_input.get("type") or "fact")
        tags = list(resolved_input.get("tags") or [])
    else:
        text, mem_type, tags = str(resolved_input or ""), "fact", []
    if not text.strip():
        raise ValueError("memory.remember needs `text` to remember")
    team_id = ensure_agent_workspace(runtime.home, runtime.home / "teams", runtime.agent)
    # D4: when approval is required, sensitive types are parked for review instead
    # of written silently. Opt-in via `memory.require_approval` (off by default).
    from jigga.runtime.memory_proposals import propose, sensitive_requires_approval
    if sensitive_requires_approval(runtime.home, mem_type):
        prop = propose(runtime.home, team_id, text=text, type=mem_type, tags=tags,
                       source={"agent": runtime.agent.id})
        append_event(runtime.logs_dir, "memory.proposed", status="ask", agent=runtime.agent.id,
                     team=team_id, proposal=prop["id"], memory_type=mem_type)
        return {"source": "capability.memory_remember", "team": team_id, "proposed": prop["id"],
                "status": "pending_approval", "text": text}
    entry = append_team_memory(runtime.home, team_id, text=text, type=mem_type, tags=tags,
                               source={"agent": runtime.agent.id})
    return {"source": "capability.memory_remember", "team": team_id, "remembered": entry["id"], "text": text}


def _draft_prompt(agent: AgentConfig, resolved_input: Any) -> tuple[str, str]:
    """Build (system, user) prompt text for a draft_with_model step.

    The agent's name/role is the system prompt. The step input is the brief:
    a bare string is used as-is; a dict's `prompt`/`brief`/`instructions` becomes
    the ask and every other key (typically prior step outputs wired in via
    `input:`) is appended as a labelled context section.
    """
    system = f"You are {agent.name}. Role: {agent.role}".strip()
    if isinstance(resolved_input, str):
        return system, resolved_input
    if isinstance(resolved_input, dict):
        data = dict(resolved_input)
        ask = data.pop("prompt", None) or data.pop("brief", None) or data.pop("instructions", None) or ""
        parts = [str(ask)] if ask else []
        for key, value in data.items():
            rendered = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
            parts.append(f"## {key}\n{rendered}")
        return system, "\n\n".join(parts) if parts else json.dumps(resolved_input, indent=2, default=str)
    return system, json.dumps(resolved_input, indent=2, default=str)


def _draft_with_model_handler(
    step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime: RuntimeContext,
) -> Any:
    """Make a workflow step *think*: route its brief through the agent's model.

    Returns the model's text directly (not a dict) so it chains cleanly — a
    downstream step's `input: {context: this_step_output.md}` receives the prose,
    and a `.md`/`.txt` `output:` writes the prose verbatim.
    """
    if runtime.agent is None:
        raise ValueError("draft_with_model requires an executing agent")
    system, brief = _draft_prompt(runtime.agent, resolved_input)
    task = {"id": f"draft_{step.id}", "title": step.action, "description": brief}
    items = [
        ModelCallItem(id=f"draft_system:{step.id}", role="system", content=system),
        ModelCallItem(id=f"step:{step.id}", role="user", content=brief),
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
    if result.status != "ok":
        raise RuntimeError(f"draft_with_model step {step.id!r} failed: {result.error}")
    return result.content


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

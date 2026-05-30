from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jigga.core.config import default_permission_mode, load_agents, load_runtime_config
from jigga.core.io import ensure_dir, write_json
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.audit import append_event, current_trace_id, new_id, trace_context
from jigga.runtime.capabilities import CapabilityManifest, CapabilityRegistry
from jigga.runtime.dispatcher import (
    RuntimeContext,
    dispatch_action,
    evaluate_capability_permissions,
)
from jigga.runtime.memory import build_context_package
from jigga.runtime.model_router import (
    ModelCallItem,
    ModelCallRequest,
    call_model,
    resolve_agent_model,
    resolve_agent_model_profile,
)
from jigga.runtime.policy import NON_EXECUTING_MODES, resolve_permission_mode
from jigga.runtime.tasks import set_task_state, tasks_for_agent

DEFAULT_MAX_TOOL_CALLS_PER_RUN = 10
DEFAULT_MAX_ITERATIONS = 8
RISKY_RISK_LEVELS = {"medium", "high"}


# --- tool schema helpers ---------------------------------------------------


def _to_tool_name(action: str) -> str:
    """OpenAI tool/function names must match ^[a-zA-Z0-9_-]+$ — no dots. Our
    action names use dots (`telegram.send_message`), so encode them. An explicit
    reverse map (built per run) makes the round-trip exact rather than relying
    on string replacement."""
    return action.replace(".", "__")


def _resolve_agent_actions(agent: AgentConfig, registry: CapabilityRegistry) -> list[str]:
    """Effective tool allowlist: `agent.tools` plus any `permissions.tools.allow`,
    filtered to actions that actually resolve to a registered capability.
    Non-resolving names (e.g. `memory.write_summary`, which isn't a capability)
    are silently skipped — they're not offered to the model."""
    allowed: list[str] = list(agent.tools or [])
    perms = agent.permissions or {}
    tools_perm = perms.get("tools") if isinstance(perms, dict) else None
    if isinstance(tools_perm, dict):
        allowed += list(tools_perm.get("allow") or [])
    resolved: list[str] = []
    for action in dict.fromkeys(allowed):  # dedupe, preserve order
        if registry.resolve_action(action) is not None:
            resolved.append(action)
    return resolved


def _build_tool_schemas(actions: list[str], registry: CapabilityRegistry) -> list[dict[str, Any]]:
    schemas: list[dict[str, Any]] = []
    for action in actions:
        capability = registry.resolve_action(action)
        if capability is None:
            continue
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": _to_tool_name(action),
                    "description": f"{capability.summary} (capability: {capability.name})",
                    # We don't ship per-action parameter schemas yet; accept an
                    # open object and let the capability handler validate input.
                    "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
                },
            }
        )
    return schemas


def _loop_limits(home: Path) -> tuple[int, int]:
    config = load_runtime_config(home)
    loop = config.get("agent_loop") or {}
    return (
        int(loop.get("max_tool_calls_per_run", DEFAULT_MAX_TOOL_CALLS_PER_RUN)),
        int(loop.get("max_iterations", DEFAULT_MAX_ITERATIONS)),
    )


def _gate_tool_call(
    capability: CapabilityManifest,
    agent: AgentConfig,
    effective_mode: str,
) -> tuple[str, str | None]:
    """Decide whether a tool call may execute. Mirrors the workflow step gate:
    - capability permission deny → ("deny", reason)
    - medium/high risk and mode != autonomous → ("needs_approval", reason)
    - otherwise ("allow", None)
    """
    decision = evaluate_capability_permissions(capability, agent)
    if decision.status != "allow":
        return ("deny", decision.reason or decision.permission)
    if capability.risk_level in RISKY_RISK_LEVELS and effective_mode != "autonomous":
        return ("needs_approval", f"capability.risk_level={capability.risk_level}")
    return ("allow", None)


# --- the per-task tool-use loop --------------------------------------------


def _system_prompt(agent: AgentConfig, memory_context: dict[str, Any]) -> str:
    base = f"You are {agent.name}. Role: {agent.role}."
    instructions = (
        " Use the available tools to accomplish the task. When the task is done, "
        "reply with a short final summary and stop calling tools."
    )
    return base + instructions


def _run_task_loop(
    *,
    home: Path,
    logs_dir: Path,
    run_dir: Path,
    run_id: str,
    agent: AgentConfig,
    task,
    effective_mode: str,
    registry: CapabilityRegistry,
    memory_context: dict[str, Any],
    runtime: RuntimeContext,
    dry_run_model: bool,
    max_tool_calls: int,
    max_iterations: int,
) -> dict[str, Any]:
    tool_actions = _resolve_agent_actions(agent, registry)
    name_map = {_to_tool_name(a): a for a in tool_actions}
    tools = _build_tool_schemas(tool_actions, registry) or None

    task_dict = task.to_dict()
    body = task_dict.get("description") or task_dict.get("title") or "No task description."
    items = [
        ModelCallItem(id="system", role="system", content=_system_prompt(agent, memory_context)),
        ModelCallItem(id=f"task:{task.id}", role="user", content=f"Task: {task.title}\n\n{body}"),
    ]

    calls_made = 0
    final_text = ""
    last_result = None
    halted: dict[str, Any] | None = None
    tool_calls_log: list[dict[str, Any]] = []

    exhausted_iterations = max_iterations <= 0
    for _iteration in range(max_iterations):
        request = ModelCallRequest(
            agent_id=agent.id,
            role=agent.role,
            task=task_dict,
            items=items,
            model=resolve_agent_model(agent),
            model_profile=resolve_agent_model_profile(agent),
            dry_run=dry_run_model,
            tools=tools,
        )
        result = call_model(home, logs_dir, request)
        last_result = result
        if result.status != "ok":
            return {"state": "failed", "final_text": "", "result": result, "calls_made": calls_made,
                    "halted": None, "tool_calls": tool_calls_log}
        if not result.tool_calls:
            final_text = result.content
            exhausted_iterations = False
            break

        # Assistant tool-call turn → echo it into the transcript.
        items.append(ModelCallItem(role="assistant", content=result.content or "", tool_calls=result.tool_calls))

        for call in result.tool_calls:
            action = name_map.get(call.name, call.name)
            append_event(logs_dir, "agent.tool_call.requested", agent=agent.id, run_id=run_id,
                         task_id=task.id, action=action, tool_call_id=call.id)

            def _tool_result(content: dict[str, Any]) -> None:
                items.append(ModelCallItem(role="tool", content=json.dumps(content, default=str), tool_call_id=call.id))

            if action not in tool_actions:
                append_event(logs_dir, "agent.tool_call.denied", status="deny", agent=agent.id, run_id=run_id,
                             task_id=task.id, action=action, reason="not in agent tool allowlist")
                _tool_result({"error": "tool not permitted for this agent", "action": action})
                continue

            capability = registry.resolve_action(action)
            verdict, reason = _gate_tool_call(capability, agent, effective_mode)
            if verdict == "needs_approval":
                append_event(logs_dir, "agent.tool_call.needs_approval", status="ask", agent=agent.id,
                             run_id=run_id, task_id=task.id, action=action, reason=reason)
                halted = {"action": action, "reason": reason}
                break
            if verdict == "deny":
                append_event(logs_dir, "agent.tool_call.denied", status="deny", agent=agent.id, run_id=run_id,
                             task_id=task.id, action=action, reason=reason)
                _tool_result({"error": "denied by policy", "action": action, "reason": reason})
                continue
            if calls_made >= max_tool_calls:
                halted = {"reason": f"max_tool_calls_per_run={max_tool_calls}"}
                append_event(logs_dir, "agent.tool_call.denied", status="deny", agent=agent.id, run_id=run_id,
                             task_id=task.id, action=action, reason=halted["reason"])
                break

            step = WorkflowStep(id=f"tool_{calls_made + 1}", action=action, input=call.arguments)
            try:
                output = dispatch_action(step, call.arguments, memory_context, runtime, registry, logs_dir, run_id=run_id)
            except Exception as exc:  # capability/handler failure → feed back, don't crash the run
                output = {"error": str(exc), "action": action}
            calls_made += 1
            tool_calls_log.append({"action": action, "arguments": call.arguments})
            append_event(logs_dir, "agent.tool_call.executed", agent=agent.id, run_id=run_id,
                         task_id=task.id, action=action, call_number=calls_made)
            _tool_result(output if isinstance(output, dict) else {"result": output})

        if halted is not None:
            exhausted_iterations = False
            break
    else:
        exhausted_iterations = True

    if exhausted_iterations:
        halted = {"reason": f"max_iterations={max_iterations}"}
        append_event(logs_dir, "agent.loop.halted", status="deny", agent=agent.id,
                     run_id=run_id, task_id=task.id, reason=halted["reason"])

    state = "needs_approval" if halted and "action" in halted else ("completed" if last_result and last_result.status == "ok" else "failed")
    # Bound exhaustion without a final answer still completes the run, but the
    # halted marker and audit event make the stop reason explicit.
    if halted and "action" not in halted:
        state = "completed"
    return {
        "state": state,
        "final_text": final_text,
        "result": last_result,
        "calls_made": calls_made,
        "halted": halted,
        "tool_calls": tool_calls_log,
    }


# --- run_agent -------------------------------------------------------------


def run_agent(
    home: Path,
    logs_dir: Path,
    tasks_dir: Path,
    agents_dir: Path,
    agent_id: str,
    dry_run_model: bool = False,
) -> dict[str, Any]:
    # Inherits the supervisor/channel trace when called from one; mints its own
    # when run standalone (CLI). Either way every event below shares the id.
    with trace_context():
        return _run_agent(home, logs_dir, tasks_dir, agents_dir, agent_id, dry_run_model)


def _run_agent(
    home: Path,
    logs_dir: Path,
    tasks_dir: Path,
    agents_dir: Path,
    agent_id: str,
    dry_run_model: bool = False,
) -> dict[str, Any]:
    agents = load_agents(agents_dir)
    agent = agents.get(agent_id)
    if agent is None:
        raise ValueError(f"Agent not found: {agent_id}")

    runtime_default = default_permission_mode(home)
    effective_mode = resolve_permission_mode(agent, runtime_default)
    run_id = new_id("agent_run")
    run_dir = home / "runs" / "agents" / agent_id / run_id
    ensure_dir(run_dir)
    pending = tasks_for_agent(tasks_dir, agent_id)
    append_event(logs_dir, "policy.evaluated", agent=agent_id, permission_mode=effective_mode,
                 runtime_default=runtime_default, agent_override=agent.permission_mode)
    append_event(logs_dir, "agent.run.started", agent=agent_id, run_id=run_id,
                 task_count=len(pending), permission_mode=effective_mode)

    if effective_mode in NON_EXECUTING_MODES:
        held: list[dict[str, Any]] = []
        for task in pending:
            updated = set_task_state(tasks_dir, task.id, "needs_approval")
            held.append(updated.to_dict())
            append_event(logs_dir, "policy.denied", status="ask", agent=agent_id, task_id=task.id,
                         permission=f"permission_mode.{effective_mode}",
                         reason=f"Agent permission_mode={effective_mode}; held without executing.")
        record = {"id": run_id, "agent_id": agent_id, "role": agent.role, "permission_mode": effective_mode,
                  "status": "policy_denied", "processed_tasks": [], "held_tasks": held, "run_dir": str(run_dir),
                  "trace_id": current_trace_id()}
        write_json(run_dir / "run.json", record)
        append_event(logs_dir, "agent.run.completed", agent=agent_id, run_id=run_id, task_count=0,
                     status="policy_denied", permission_mode=effective_mode, held_task_count=len(held))
        return record

    registry = CapabilityRegistry.load(user_capabilities=home / "capabilities", approvals_dir=home / "policies")
    runtime = RuntimeContext(agent=agent, home=home, logs_dir=logs_dir, sessions_dir=home / "sessions")
    scope = agent.memory_scope or "task_only"
    memory_context = build_context_package(home / "memory", scope)
    max_tool_calls, max_iterations = _loop_limits(home)

    processed: list[dict[str, Any]] = []
    for task in pending:
        set_task_state(tasks_dir, task.id, "claimed")
        set_task_state(tasks_dir, task.id, "running")
        loop = _run_task_loop(
            home=home, logs_dir=logs_dir, run_dir=run_dir, run_id=run_id, agent=agent, task=task,
            effective_mode=effective_mode, registry=registry, memory_context=memory_context,
            runtime=runtime, dry_run_model=dry_run_model, max_tool_calls=max_tool_calls,
            max_iterations=max_iterations,
        )
        result = loop["result"]
        artifact = {
            "task_id": task.id,
            "agent_id": agent_id,
            "title": task.title,
            "permission_mode": effective_mode,
            "model": result.to_dict() if result else None,
            "result": loop["final_text"],
            "tool_calls": loop["tool_calls"],
            "halted": loop["halted"],
            "trace_id": current_trace_id(),
        }
        write_json(run_dir / f"{task.id}.json", artifact)
        completed = set_task_state(tasks_dir, task.id, loop["state"])
        processed.append(completed.to_dict())

        if loop["state"] == "completed":
            append_event(logs_dir, "task.completed", agent=agent_id, task_id=task.id, title=task.title, run_id=run_id)
            append_event(logs_dir, "agent.task_completed", agent_id=agent_id, task_id=task.id,
                         title=task.title, run_id=run_id)
        elif loop["state"] == "needs_approval":
            append_event(logs_dir, "task.needs_approval", status="ask", agent=agent_id, task_id=task.id,
                         run_id=run_id, reason=(loop["halted"] or {}).get("reason"))

    run_record = {
        "id": run_id,
        "agent_id": agent_id,
        "role": agent.role,
        "permission_mode": effective_mode,
        "processed_tasks": processed,
        "run_dir": str(run_dir),
        "trace_id": current_trace_id(),
    }
    write_json(run_dir / "run.json", run_record)
    append_event(logs_dir, "agent.run.completed", agent=agent_id, run_id=run_id, task_count=len(processed))
    return run_record

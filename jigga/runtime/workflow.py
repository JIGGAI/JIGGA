from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import default_permission_mode, load_agents, load_workflows
from jigga.core.io import ensure_dir, write_json
from jigga.core.models import AgentConfig, WorkflowConfig, WorkflowStep, now_iso
from jigga.core.paths import JiggaPaths
from jigga.runtime.audit import append_event, current_trace_id, new_id, trace_context
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.dispatcher import RuntimeContext, evaluate_capability_permissions, execute_step
from jigga.runtime.memory import build_context_package, write_memory_result
from jigga.runtime.policy import evaluate_workflow_step, resolve_permission_mode


def _required_permissions(workflow: WorkflowConfig) -> list[str]:
    required = workflow.permissions.get("required", [])
    return list(required) if isinstance(required, list) else []


def _step_policy(
    step: WorkflowStep,
    workflow: WorkflowConfig,
    agents: dict[str, AgentConfig],
    default_mode: str,
    registry: CapabilityRegistry | None = None,
) -> dict[str, str | None]:
    agent = agents.get(step.agent or "")
    decision = evaluate_workflow_step(step, agent, default_mode=default_mode)
    status = {"ask": "needs_approval", "deny": "blocked", "allow": "allow"}[decision.status]
    if decision.status == "allow" and step.agent and step.agent not in agents and step.optional:
        status = "skipped"
    mode = resolve_permission_mode(agent, default_mode) if agent else None
    capability = registry.resolve_action(step.action) if registry else None
    if status == "allow" and registry is not None and capability is None:
        status = "blocked"
        reason = f"No capability registered for workflow action: {step.action}"
        permission = "capability.available"
    elif status == "allow" and capability is not None and capability.risk_level in {"medium", "high"} and mode != "autonomous":
        status = "needs_approval"
        reason = f"Capability {capability.name} risk_level={capability.risk_level} requires approval."
        permission = "capability.risk_level"
    elif status == "allow" and capability is not None:
        capability_decision = evaluate_capability_permissions(capability, agent)
        if capability_decision.status == "allow":
            reason = decision.reason
            permission = decision.permission
        else:
            status = {"ask": "needs_approval", "deny": "blocked"}[capability_decision.status]
            reason = capability_decision.reason
            permission = capability_decision.permission
    else:
        reason = decision.reason
        permission = decision.permission
    return {
        "status": status,
        "reason": reason,
        "permission": permission,
        "permission_mode": mode,
        "capability": capability.name if capability else None,
        "risk_level": capability.risk_level if capability else None,
    }


def _plan_workflow_v2(
    workflow: WorkflowConfig,
    agents: dict[str, AgentConfig],
    default_mode: str,
    registry: CapabilityRegistry | None,
) -> dict[str, Any]:
    """Plan for a DAG workflow. Unlike v1, `needs_approval` does not make the
    plan unrunnable — the runner parks at that node and asks on the channel —
    so `can_run` only fails on structural problems or blocked nodes."""
    from jigga.core.models import WorkflowStep as _Step
    from jigga.runtime.workflow_engine import validate_graph

    problems = validate_graph(workflow)
    nodes = []
    for node in workflow.nodes:
        if node.type == "human_approval":
            policy: dict[str, Any] = {"status": "needs_approval", "reason": "human approval node",
                                      "permission": None, "permission_mode": None,
                                      "capability": None, "risk_level": None}
        else:
            action = node.action or ("draft_with_model" if node.type == "llm" else "")
            step = _Step(id=node.id, action=action, agent=node.agent,
                         input=dict(node.input or {}), output=node.output, optional=node.optional)
            if node.type == "writeback":
                policy = {"status": "allow", "reason": "workspace writeback", "permission": None,
                          "permission_mode": None, "capability": None, "risk_level": None}
            else:
                policy = _step_policy(step, workflow, agents, default_mode, registry)
        nodes.append({"id": node.id, "type": node.type, "agent": node.agent, "action": node.action,
                      "output": node.output, "optional": node.optional, "policy": policy})
    return {
        "workflow": {
            "id": workflow.id,
            "name": workflow.name,
            "purpose": workflow.purpose,
            "status": workflow.status,
            "source": workflow.source,
        },
        "engine": "v2",
        "trigger": workflow.trigger,
        "permissions": _required_permissions(workflow),
        "memory": workflow.memory,
        "default_permission_mode": default_mode,
        "problems": problems,
        "nodes": nodes,
        "edges": workflow.edges,
        "can_run": not problems and all(n["policy"]["status"] != "blocked" for n in nodes),
    }


def plan_workflow(
    workflow: WorkflowConfig,
    agents: dict[str, AgentConfig],
    default_mode: str = "ask",
    registry: CapabilityRegistry | None = None,
) -> dict[str, Any]:
    if workflow.nodes:
        return _plan_workflow_v2(workflow, agents, default_mode, registry)
    steps = []
    for step in workflow.steps:
        steps.append(
            {
                "id": step.id,
                "agent": step.agent,
                "action": step.action,
                "output": step.output,
                "optional": step.optional,
                "approval": step.approval or "not_required",
                "policy": _step_policy(step, workflow, agents, default_mode, registry),
            }
        )
    return {
        "workflow": {
            "id": workflow.id,
            "name": workflow.name,
            "purpose": workflow.purpose,
            "status": workflow.status,
            "source": workflow.source,
        },
        "trigger": workflow.trigger,
        "permissions": _required_permissions(workflow),
        "memory": workflow.memory,
        "default_permission_mode": default_mode,
        "steps": steps,
        "can_run": all(step["policy"]["status"] in {"allow", "skipped"} for step in steps),
    }



def run_workflow(
    paths: JiggaPaths,
    workflow_id: str,
    project_capabilities: Path | None = None,
) -> dict[str, Any]:
    # Inherits the supervisor trace when scheduled; mints its own on CLI apply.
    with trace_context():
        return _run_workflow(paths, workflow_id, project_capabilities)


def _run_workflow(
    paths: JiggaPaths,
    workflow_id: str,
    project_capabilities: Path | None = None,
) -> dict[str, Any]:
    home, logs_dir, workflows_dir, agents_dir, memory_dir = (
        paths.home, paths.logs, paths.workflows, paths.agents, paths.memory,
    )
    agents = load_agents(agents_dir)
    workflows = load_workflows(workflows_dir)
    registry = CapabilityRegistry.load(
        user_capabilities=home / "capabilities",
        project_capabilities=project_capabilities,
        approvals_dir=home / "policies",
    )
    workflow = workflows.get(workflow_id)
    if workflow is None:
        raise ValueError(f"Workflow not found: {workflow_id}")

    if workflow.nodes:
        # DAG workflow → the v2 engine (persisted, resumable run state).
        # Imported lazily: workflow_engine reuses this module's policy helper.
        from jigga.runtime.workflow_engine import run_workflow_v2

        return run_workflow_v2(paths, workflow, project_capabilities=project_capabilities)

    plan = plan_workflow(workflow, agents, default_mode=default_permission_mode(home), registry=registry)
    if not plan["can_run"]:
        status = "needs_approval" if any(step["policy"]["status"] == "needs_approval" for step in plan["steps"]) else "blocked"
        append_event(logs_dir, "workflow.plan_blocked", status=status, workflow=workflow_id, plan=plan)
        return {"status": status, "plan": plan}

    run_id = new_id("workflow_run")
    run_dir = home / "runs" / "workflows" / workflow_id / run_id
    ensure_dir(run_dir)
    outputs: dict[str, Any] = {}
    started_at = now_iso()
    append_event(logs_dir, "workflow.run.started", workflow=workflow_id, run_id=run_id)

    for step, planned in zip(workflow.steps, plan["steps"], strict=True):
        if planned["policy"]["status"] == "skipped":
            append_event(logs_dir, "workflow.step.skipped", workflow=workflow_id, run_id=run_id, step=step.id)
            continue
        agent = agents.get(step.agent or "")
        scope = agent.memory_scope if agent and agent.memory_scope else "task_only"
        memory_context = build_context_package(memory_dir, scope)
        runtime = RuntimeContext(
            agent=agent,
            home=home,
            logs_dir=logs_dir,
            sessions_dir=home / "sessions",
        )
        append_event(logs_dir, "workflow.step.started", workflow=workflow_id, run_id=run_id, step=step.id, agent=step.agent)
        output, artifact = execute_step(
            step, run_dir, outputs, memory_context, runtime, registry, logs_dir, workflow_id, run_id
        )
        outputs[step.id] = output
        if step.output:
            outputs[step.output] = output
        append_event(
            logs_dir,
            "workflow.step.completed",
            workflow=workflow_id,
            run_id=run_id,
            step=step.id,
            artifact=str(artifact) if artifact else None,
        )

    memory_artifact = None
    if workflow.memory.get("write_summary") or workflow.memory.get("write_raw"):
        memory_artifact = write_memory_result(
            memory_dir,
            logs_dir,
            "workflow_result",
            {"workflow": workflow_id, "outputs": outputs},
            {"workflow": workflow_id, "run_id": run_id},
        )

    record = {
        "id": run_id,
        "workflow_id": workflow_id,
        "status": "completed",
        "started_at": started_at,
        "completed_at": now_iso(),
        "run_dir": str(run_dir),
        "outputs": outputs,
        "memory_artifact": str(memory_artifact) if memory_artifact else None,
        "trace_id": current_trace_id(),
    }
    write_json(run_dir / "run.json", record)
    append_event(logs_dir, "workflow.run.completed", workflow=workflow_id, run_id=run_id)
    append_event(logs_dir, "workflow.completed", workflow=workflow_id, run_id=run_id, title=workflow.name)
    return record

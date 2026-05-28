from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_agents, load_workflows
from jigga.core.io import ensure_dir, write_json
from jigga.core.models import AgentConfig, WorkflowConfig, WorkflowStep, now_iso
from jigga.runtime.audit import append_event, new_id
from jigga.runtime.memory import build_context_package, write_memory_result


def _required_permissions(workflow: WorkflowConfig) -> list[str]:
    required = workflow.permissions.get("required", [])
    return list(required) if isinstance(required, list) else []


def _step_policy(step: WorkflowStep, workflow: WorkflowConfig, agents: dict[str, AgentConfig]) -> dict[str, str | None]:
    if step.approval == "required":
        return {"status": "needs_approval", "reason": f"Step {step.id} requires approval."}
    if step.agent and step.agent not in agents:
        if step.optional:
            return {"status": "skipped", "reason": f"Optional agent {step.agent} is not configured."}
        return {"status": "blocked", "reason": f"Agent {step.agent} is not configured."}
    return {"status": "allow", "reason": None}


def plan_workflow(workflow: WorkflowConfig, agents: dict[str, AgentConfig]) -> dict[str, Any]:
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
                "policy": _step_policy(step, workflow, agents),
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
        "steps": steps,
        "can_run": all(step["policy"]["status"] in {"allow", "skipped"} for step in steps),
    }


def _resolve(value: Any, outputs: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return outputs.get(value, value)
    if isinstance(value, list):
        return [_resolve(item, outputs) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, outputs) for key, item in value.items()}
    return value


def _execute_step(step: WorkflowStep, run_dir: Path, outputs: dict[str, Any], context: dict[str, Any]) -> tuple[Any, Path | None]:
    ensure_dir(run_dir)
    resolved_input = _resolve(step.input, outputs)
    if step.action == "calendar.list_events":
        output = [
            {"time": "09:30", "title": "Planning block", "source": "stub"},
            {"time": "14:00", "title": "Project review", "source": "stub"},
        ]
        return output, None
    if step.action == "email.search":
        output = [{"from": "client@example.com", "subject": "Launch follow-up", "source": "stub"}]
        return output, None
    if step.action in {"summarize_day", "summarize_relevant_context"}:
        output = {
            "summary": f"MVP summary for {step.id}",
            "input": resolved_input,
            "memory_context": context,
        }
    elif step.action == "notifications.send":
        output = {"dry_run": True, "delivered": False, "input": resolved_input}
    else:
        output = {"dry_run": True, "action": step.action, "input": resolved_input}

    artifact = None
    if step.output:
        artifact = run_dir / step.output
        if artifact.suffix in {".md", ".txt"}:
            artifact.write_text(str(output), encoding="utf-8")
        else:
            write_json(artifact, output)
    return output, artifact


def run_workflow(home: Path, logs_dir: Path, workflows_dir: Path, agents_dir: Path, memory_dir: Path, workflow_id: str) -> dict[str, Any]:
    agents = load_agents(agents_dir)
    workflows = load_workflows(workflows_dir)
    workflow = workflows.get(workflow_id)
    if workflow is None:
        raise ValueError(f"Workflow not found: {workflow_id}")

    plan = plan_workflow(workflow, agents)
    if not plan["can_run"]:
        append_event(logs_dir, "workflow.plan_blocked", status="needs_approval", workflow=workflow_id, plan=plan)
        return {"status": "blocked", "plan": plan}

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
        context = build_context_package(memory_dir, scope)
        append_event(logs_dir, "workflow.step.started", workflow=workflow_id, run_id=run_id, step=step.id, agent=step.agent)
        output, artifact = _execute_step(step, run_dir, outputs, context)
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
    }
    write_json(run_dir / "run.json", record)
    append_event(logs_dir, "workflow.run.completed", workflow=workflow_id, run_id=run_id)
    return record

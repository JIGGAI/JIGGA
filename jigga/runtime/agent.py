from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import default_permission_mode, load_agents
from jigga.core.io import ensure_dir, write_json
from jigga.runtime.audit import append_event, new_id
from jigga.runtime.model_router import build_task_model_request, call_model
from jigga.runtime.policy import NON_EXECUTING_MODES, resolve_permission_mode
from jigga.runtime.tasks import set_task_state, tasks_for_agent


def run_agent(
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
    append_event(
        logs_dir,
        "policy.evaluated",
        agent=agent_id,
        permission_mode=effective_mode,
        runtime_default=runtime_default,
        agent_override=agent.permission_mode,
    )
    append_event(
        logs_dir,
        "agent.run.started",
        agent=agent_id,
        run_id=run_id,
        task_count=len(pending),
        permission_mode=effective_mode,
    )

    if effective_mode in NON_EXECUTING_MODES:
        held: list[dict[str, Any]] = []
        for task in pending:
            updated = set_task_state(tasks_dir, task.id, "needs_approval")
            held.append(updated.to_dict())
            append_event(
                logs_dir,
                "policy.denied",
                status="ask",
                agent=agent_id,
                task_id=task.id,
                permission=f"permission_mode.{effective_mode}",
                reason=f"Agent permission_mode={effective_mode}; held without executing.",
            )
        record = {
            "id": run_id,
            "agent_id": agent_id,
            "role": agent.role,
            "permission_mode": effective_mode,
            "status": "policy_denied",
            "processed_tasks": [],
            "held_tasks": held,
            "run_dir": str(run_dir),
        }
        write_json(run_dir / "run.json", record)
        append_event(
            logs_dir,
            "agent.run.completed",
            agent=agent_id,
            run_id=run_id,
            task_count=0,
            status="policy_denied",
            permission_mode=effective_mode,
            held_task_count=len(held),
        )
        return record

    processed: list[dict[str, Any]] = []
    for task in pending:
        set_task_state(tasks_dir, task.id, "claimed")
        set_task_state(tasks_dir, task.id, "running")
        request = build_task_model_request(agent, task.to_dict(), dry_run=dry_run_model)
        model_result = call_model(home, logs_dir, request)
        artifact = {
            "task_id": task.id,
            "agent_id": agent_id,
            "title": task.title,
            "permission_mode": effective_mode,
            "model": model_result.to_dict(),
            "result": model_result.content,
        }
        write_json(run_dir / f"{task.id}.json", artifact)
        next_state = "completed" if model_result.status == "ok" else "failed"
        completed = set_task_state(tasks_dir, task.id, next_state)
        processed.append(completed.to_dict())
        append_event(logs_dir, "task.completed", agent=agent_id, task_id=task.id, title=task.title, run_id=run_id)
        append_event(
            logs_dir,
            "agent.task_completed",
            agent_id=agent_id,
            task_id=task.id,
            title=task.title,
            run_id=run_id,
        )

    run_record = {
        "id": run_id,
        "agent_id": agent_id,
        "role": agent.role,
        "permission_mode": effective_mode,
        "processed_tasks": processed,
        "run_dir": str(run_dir),
    }
    write_json(run_dir / "run.json", run_record)
    append_event(logs_dir, "agent.run.completed", agent=agent_id, run_id=run_id, task_count=len(processed))
    return run_record

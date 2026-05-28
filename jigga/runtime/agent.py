from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_agents
from jigga.core.io import ensure_dir, write_json
from jigga.runtime.audit import append_event, new_id
from jigga.runtime.tasks import set_task_state, tasks_for_agent


def run_agent(home: Path, logs_dir: Path, tasks_dir: Path, agents_dir: Path, agent_id: str) -> dict[str, Any]:
    agents = load_agents(agents_dir)
    agent = agents.get(agent_id)
    if agent is None:
        raise ValueError(f"Agent not found: {agent_id}")

    run_id = new_id("agent_run")
    run_dir = home / "runs" / "agents" / agent_id / run_id
    ensure_dir(run_dir)
    pending = tasks_for_agent(tasks_dir, agent_id)
    append_event(logs_dir, "agent.run.started", agent=agent_id, run_id=run_id, task_count=len(pending))

    processed: list[dict[str, Any]] = []
    for task in pending:
        set_task_state(tasks_dir, task.id, "claimed")
        set_task_state(tasks_dir, task.id, "running")
        artifact = {
            "task_id": task.id,
            "agent_id": agent_id,
            "title": task.title,
            "result": "MVP runner acknowledged task. Model/tool execution is not implemented yet.",
        }
        write_json(run_dir / f"{task.id}.json", artifact)
        completed = set_task_state(tasks_dir, task.id, "completed")
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
        "processed_tasks": processed,
        "run_dir": str(run_dir),
    }
    write_json(run_dir / "run.json", run_record)
    append_event(logs_dir, "agent.run.completed", agent=agent_id, run_id=run_id, task_count=len(processed))
    return run_record

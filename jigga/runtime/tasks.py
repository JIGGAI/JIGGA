from __future__ import annotations

from pathlib import Path

from jigga.core.io import ensure_dir, read_json, write_json
from jigga.core.models import Task, now_iso, validate_task_state
from jigga.runtime.audit import new_id


def create_task(
    tasks_dir: Path,
    title: str,
    description: str | None = None,
    assignee: str | None = None,
    workflow_id: str | None = None,
    metadata: dict | None = None,
) -> Task:
    ensure_dir(tasks_dir)
    task = Task(
        id=new_id("task"),
        title=title,
        description=description,
        assignee=assignee,
        workflow_id=workflow_id,
        metadata=metadata or {},
    )
    write_task(tasks_dir, task)
    return task


def write_task(tasks_dir: Path, task: Task) -> None:
    write_json(tasks_dir / f"{task.id}.json", task.to_dict())


def list_tasks(tasks_dir: Path) -> list[Task]:
    if not tasks_dir.exists():
        return []
    tasks = [Task.from_dict(read_json(file)) for file in sorted(tasks_dir.glob("*.json"))]
    return sorted(tasks, key=lambda item: item.created_at)


def tasks_for_agent(tasks_dir: Path, agent_id: str) -> list[Task]:
    return [task for task in list_tasks(tasks_dir) if task.assignee == agent_id and task.state == "pending"]


def find_task(tasks_dir: Path, task_id: str) -> Task | None:
    return next((task for task in list_tasks(tasks_dir) if task.id == task_id or task.id.startswith(task_id)), None)


def set_task_state(tasks_dir: Path, task_id: str, state: str) -> Task:
    task = find_task(tasks_dir, task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")
    task.state = validate_task_state(state)
    task.updated_at = now_iso()
    write_task(tasks_dir, task)
    return task

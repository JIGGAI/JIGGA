"""Task store + a lightweight index (Hardening H1b).

Tasks are one JSON file each under `tasks_dir/`. The hot paths — the supervisor
picking pending assignees every tick, an agent claiming its pending tasks,
`set_task_state` resolving an id — used to glob and parse *every* task file on
each call (O(all tasks) per state change and per tick).

A derived index at `state/tasks/index.json` maps `task_id -> {state, assignee,
created_at}` so those lookups read only the files they need. The index is
self-healing: it's rebuilt from disk whenever it's missing, and readers tolerate
entries whose file has since been archived/removed (the task files remain the
source of truth). `list_tasks` keeps the full scan — it's the cold reporting
path that wants every task.
"""

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
    index = _ensure_index(tasks_dir)
    index[task.id] = _index_entry(task)
    _write_index(tasks_dir, index)


def list_tasks(tasks_dir: Path) -> list[Task]:
    if not tasks_dir.exists():
        return []
    tasks = [Task.from_dict(read_json(file)) for file in sorted(tasks_dir.glob("*.json"))]
    return sorted(tasks, key=lambda item: item.created_at)


def tasks_for_agent(tasks_dir: Path, agent_id: str) -> list[Task]:
    """An agent's pending tasks. Reads only the matching task files (located via
    the index) rather than scanning the whole store."""
    index = _ensure_index(tasks_dir)
    matched: list[Task] = []
    for task_id, meta in index.items():
        if meta.get("assignee") == agent_id and meta.get("state") == "pending":
            task = _load_task(tasks_dir, task_id)
            if task is not None:
                matched.append(task)
    return sorted(matched, key=lambda item: item.created_at)


def find_task(tasks_dir: Path, task_id: str) -> Task | None:
    """Resolve a task by exact id or id-prefix. Consults the index to avoid
    reading every file; ties break by creation time (as `list_tasks` would)."""
    index = _ensure_index(tasks_dir)
    candidates = [tid for tid in index if tid == task_id or tid.startswith(task_id)]
    candidates.sort(key=lambda tid: index[tid].get("created_at") or "")
    for tid in candidates:
        task = _load_task(tasks_dir, tid)
        if task is not None:
            return task
    return None


def set_task_state(tasks_dir: Path, task_id: str, state: str) -> Task:
    task = find_task(tasks_dir, task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")
    task.state = validate_task_state(state)
    task.updated_at = now_iso()
    write_task(tasks_dir, task)
    return task


def pending_summary(tasks_dir: Path) -> tuple[list[str], int]:
    """(sorted assignees with pending tasks, count of pending tasks), computed
    from the index alone — no task-file reads. For the supervisor tick."""
    index = _ensure_index(tasks_dir)
    pending = [meta for meta in index.values() if meta.get("state") == "pending"]
    targets = sorted({meta["assignee"] for meta in pending if meta.get("assignee")})
    return targets, len(pending)


def forget_tasks(tasks_dir: Path, task_ids: list[str]) -> None:
    """Drop entries from the index (e.g. after compaction archives their files)."""
    if not task_ids:
        return
    index = _ensure_index(tasks_dir)
    drop = {tid[:-5] if tid.endswith(".json") else tid for tid in task_ids}
    changed = False
    for tid in drop:
        if tid in index:
            del index[tid]
            changed = True
    if changed:
        _write_index(tasks_dir, index)


# --- index internals -------------------------------------------------------


def _index_path(tasks_dir: Path) -> Path:
    return Path(tasks_dir).parent / "state" / "tasks" / "index.json"


def _index_entry(task: Task) -> dict:
    return {"state": task.state, "assignee": task.assignee, "created_at": task.created_at}


def _load_task(tasks_dir: Path, task_id: str) -> Task | None:
    path = Path(tasks_dir) / f"{task_id}.json"
    if not path.exists():
        return None
    return Task.from_dict(read_json(path))


def _write_index(tasks_dir: Path, index: dict) -> None:
    write_json(_index_path(tasks_dir), index)


def rebuild_index(tasks_dir: Path) -> dict:
    """Rebuild the index from the task files on disk (the source of truth)."""
    index: dict[str, dict] = {}
    if Path(tasks_dir).exists():
        for file in sorted(Path(tasks_dir).glob("*.json")):
            try:
                task = Task.from_dict(read_json(file))
            except (OSError, ValueError, KeyError):
                continue
            index[task.id] = _index_entry(task)
    _write_index(tasks_dir, index)
    return index


def _ensure_index(tasks_dir: Path) -> dict:
    path = _index_path(tasks_dir)
    if path.exists():
        try:
            index = read_json(path)
        except (ValueError, OSError):
            index = None
        if isinstance(index, dict):
            return index
        # corrupt/wrong-shape index → rebuild from the task files (source of
        # truth) rather than crash the supervisor's per-tick lookups
        return rebuild_index(tasks_dir)
    return rebuild_index(tasks_dir)

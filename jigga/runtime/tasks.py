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

import re
import shutil
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
    lane: str | None = None,
) -> Task:
    ensure_dir(tasks_dir)
    task = Task(
        id=new_id("task"),
        title=title,
        description=description,
        assignee=assignee,
        workflow_id=workflow_id,
        lane=lane,
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


# Sentinel for "this field was not passed". `None` is a real value here — it is
# how you clear a description or unassign a ticket — so it cannot double as
# "leave unchanged".
_UNSET = object()


def update_task(
    tasks_dir: Path,
    task_id: str,
    *,
    title: str | object = _UNSET,
    description: str | None | object = _UNSET,
    assignee: str | None | object = _UNSET,
    state: str | object = _UNSET,
    lane: str | None | object = _UNSET,
    metadata: dict | object = _UNSET,
) -> Task:
    """Edit a task's fields. Only the fields you pass change.

    `state` and `lane` have their own gated entry points for ordinary callers
    (`set_task_state`, `move_task_lane`) — lane moves in particular are gated
    per team, and a generic setter must not become a quiet way around that
    gate. `state`/`lane`/`metadata` are accepted here only for a caller that
    has already resolved the destination itself (e.g. the ticket-outcome
    helper in `runtime/agent.py`, which computes the gated transition before
    calling this) and wants to write several fields as one update.
    """
    task = find_task(tasks_dir, task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")
    if title is not _UNSET:
        cleaned = str(title).strip()
        if not cleaned:
            raise ValueError("Title cannot be empty")
        task.title = cleaned
    if description is not _UNSET:
        task.description = None if description is None else str(description)
    if assignee is not _UNSET:
        # "" clears the assignee; the CLI passes None to mean "leave alone".
        cleaned = None if assignee is None else str(assignee).strip()
        task.assignee = cleaned or None
    if state is not _UNSET:
        task.state = validate_task_state(state)
    if lane is not _UNSET:
        task.lane = None if lane is None else str(lane)
    if metadata is not _UNSET:
        task.metadata = dict(metadata) if metadata else {}
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


def _archived_path(tasks_dir: Path, task_id: str) -> Path:
    return Path(tasks_dir) / "archive" / f"{task_id}.json"


def archive_task(tasks_dir: Path, task_id: str) -> Task:
    """Take a task out of the live set, keeping the file.

    The same retirement compaction already performs on old finished tasks:
    the file moves to `tasks/archive/` and the id leaves the index, so it stops
    appearing on boards and in `list_tasks` but is still on disk to restore.
    """
    task = find_task(tasks_dir, task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")
    source = Path(tasks_dir) / f"{task.id}.json"
    if source.exists():
        destination = _archived_path(tasks_dir, task.id)
        ensure_dir(destination.parent)
        shutil.move(str(source), str(destination))
    forget_tasks(tasks_dir, [task.id])
    return task


def destroy_task(tasks_dir: Path, task_id: str) -> Task:
    """Delete a task's file outright. Unlike `archive_task`, nothing survives.

    Accepts an already-archived id as well as a live one — otherwise archiving
    would be a one-way door and `tasks/archive/` could only ever be emptied by
    hand.
    """
    task = find_task(tasks_dir, task_id)
    path = Path(tasks_dir) / f"{task_id}.json"
    if task is None:
        archived = _archived_path(tasks_dir, task_id)
        if not archived.exists():
            raise ValueError(f"Task not found: {task_id}")
        task = Task.from_dict(read_json(archived))
        path = archived
    path.unlink(missing_ok=True)
    forget_tasks(tasks_dir, [task_id])
    return task


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


# --- ticket-level approval opt-in ------------------------------------------

# A ticket writer asks for a human gate by putting a directive line in the
# ticket body. Everything else runs on its own — the pipeline should not stop
# for work nobody asked to review.
#
# Matched on its own line so ordinary prose ("ask the client for approval")
# cannot accidentally park a ticket. The value must be an affirmative word;
# `Approval: not required` reads as a deliberate opt-OUT and is honoured.
_APPROVAL_DIRECTIVE = re.compile(
    r"^\s*approval\s*:\s*(?P<value>[a-z ]+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_AFFIRMATIVE = {"required", "yes", "true", "needed", "require"}


def task_requires_approval(task) -> bool:
    """True when this ticket's writer explicitly asked for a human gate.

    Read at gate time rather than at creation time so it works no matter how
    the ticket was filed — CLI, web board, or an agent creating one — without
    every creation path having to know about it.
    """
    metadata = getattr(task, "metadata", None) or {}
    if metadata.get("requires_approval"):
        return True
    description = getattr(task, "description", None) or ""
    for match in _APPROVAL_DIRECTIVE.finditer(description):
        if match.group("value").strip().lower() in _AFFIRMATIVE:
            return True
    return False

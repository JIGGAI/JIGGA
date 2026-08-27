"""Crash recovery — sweep half-states left by a supervisor that died mid-tick
(roadmap "production needs" item 8).

Two wedge states exist, and both are invisible until you hit them:

1. **Tasks stuck `claimed`/`running`.** The agent loop sets task state before
   executing; a crash between the transition and completion strands the task —
   it never re-enters `pending_summary`, so the agent is never woken for it
   again.
2. **v2 workflow nodes stuck `running`.** `_execute_node` persists
   `status: running` before dispatch; the DAG sweep only considers `pending`
   nodes, so a crash mid-node wedges the whole run in `running` forever.

The sweep marks both `failed` past a staleness threshold (default 120 min,
config `recovery.max_stale_minutes`) with an audit event — visible, never
silently retried: a half-executed side effect (an email half-sent, a command
half-run) must not be blindly re-executed; a failed node's error edges can
route recovery explicitly, and a failed task is the user/agent's call to
re-queue. Age-filtered, so running it every tick is idempotent and in-flight
work (seconds old) is never touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jigga.core.config import load_runtime_config
from jigga.core.io import write_json
from jigga.core.models import now_iso
from jigga.core.paths import JiggaPaths
from jigga.runtime.audit import append_event
from jigga.runtime.decompose import release_parent_if_ready
from jigga.runtime.tasks import list_tasks, set_task_state

DEFAULT_MAX_STALE_MINUTES = 120


def _max_stale_minutes(home: Path) -> int:
    recovery = load_runtime_config(home).get("recovery") or {}
    return int(recovery.get("max_stale_minutes", DEFAULT_MAX_STALE_MINUTES))


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def held_leases(paths: JiggaPaths, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every task and v2 node currently holding a claim, with when it expires.

    The expiry was always implicit — `updated_at` plus a threshold buried in
    config — so a stalled run looked mysterious rather than merely stuck. This
    makes it a number you can read.

    `expired: True` on a live system means the sweep that should have released
    it hasn't run, which almost always means the supervisor is down. That is a
    more useful signal than the stall itself.
    """
    now = now or datetime.now(timezone.utc)
    ttl = timedelta(minutes=_max_stale_minutes(paths.home))
    rows: list[dict[str, Any]] = []

    def _row(kind: str, ident: str, holder: str | None, since: str | None) -> dict[str, Any]:
        held_since = _parse(since)
        expires = held_since + ttl if held_since else None
        return {
            "kind": kind,
            "id": ident,
            "holder": holder,
            "held_since": since,
            "expires_at": expires.isoformat() if expires else None,
            "expired": bool(expires and expires <= now),
            "seconds_remaining": int((expires - now).total_seconds()) if expires else None,
        }

    for task in list_tasks(paths.tasks):
        if task.state in ("claimed", "running"):
            rows.append(_row("task", task.id, task.assignee, task.updated_at))

    from jigga.runtime.workflow_engine import list_runs

    for record in list_runs(paths, active_only=True):
        for node_id, state in (record.get("nodes") or {}).items():
            if state.get("status") == "running":
                rows.append(_row("node", f"{record.get('id')}:{node_id}",
                                 record.get("workflow_id"), state.get("started_at")))
    return rows


def sweep_stale(paths: JiggaPaths, *, now: datetime | None = None) -> dict[str, Any]:
    """Recover tasks and v2 workflow nodes stranded longer than the threshold.
    Returns {"tasks": [ids], "nodes": ["run_id:node_id"]} of what was recovered."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=_max_stale_minutes(paths.home))
    recovered_tasks: list[str] = []
    for task in list_tasks(paths.tasks):
        if task.state not in ("claimed", "running"):
            continue
        updated = _parse(task.updated_at)
        if updated is not None and updated < cutoff:
            set_task_state(paths.tasks, task.id, "failed")
            append_event(paths.logs, "task.recovered", status="failed", task_id=task.id,
                         agent=task.assignee, stale_state=task.state, stale_since=task.updated_at)
            # A swept story is dead for good — nothing re-queues a `failed`
            # task — so an epic waiting on it would wait forever unless the
            # sweep releases it here, exactly as a finished run does.
            released = release_parent_if_ready(paths.tasks, paths.teams, task.id)
            if released:
                append_event(paths.logs, "ticket.epic.released", task_id=released["epic"],
                             child=task.id, child_state="failed", reason=released["reason"])
            recovered_tasks.append(task.id)

    recovered_nodes: list[str] = []
    from jigga.runtime.workflow_engine import list_runs

    for record in list_runs(paths, active_only=True):
        changed = False
        for node_id, state in (record.get("nodes") or {}).items():
            started = _parse(state.get("started_at"))
            if state.get("status") == "running" and started is not None and started < cutoff:
                state["status"] = "failed"
                state["error"] = "interrupted — recovered by crash sweep (side effects unknown)"
                state["completed_at"] = now_iso()
                append_event(paths.logs, "workflow.node.recovered", status="failed",
                             workflow=record.get("workflow_id"), run_id=record.get("id"),
                             node=node_id, stale_since=state.get("started_at"))
                recovered_nodes.append(f"{record.get('id')}:{node_id}")
                changed = True
        if changed:
            # Persist directly; the next advance_all_runs pass finalizes the run
            # (error edges fire, or the run fails with an unhandled failure).
            record["updated_at"] = now_iso()
            write_json(Path(record["run_dir"]) / "run.json", record)

    return {"tasks": recovered_tasks, "nodes": recovered_nodes}

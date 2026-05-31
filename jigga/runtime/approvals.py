"""File-first approval queue (Milestone B, slice B6).

When an agent action needs human approval (medium/high-risk capability, agent
not in `autonomous` mode), the runtime parks a pending approval here and asks on
the originating channel. The user replies `approve <code>` / `deny <code>`; the
channel gateway resolves it and re-queues the held task. On the re-run the gate
finds the approval and lets that one action through (consumed once).

Mirrors ClawRecipes' `approval.json`: durable, inspectable, code-gated. Records
live under `~/.jigga/approvals/queue/<id>.json`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, read_json, write_json
from jigga.core.models import now_iso
from jigga.runtime.audit import new_id
from jigga.runtime.tasks import set_task_state

_QUEUE = "queue"
# `approve <CODE>` / `deny <CODE>` / `decline <CODE>` — case-insensitive.
_REPLY = re.compile(r"^\s*(approve|approved|yes|deny|denied|decline|reject|no)\s+([A-Za-z0-9]{4,16})\s*$", re.I)
_APPROVE_WORDS = {"approve", "approved", "yes"}


def _queue_dir(approvals_dir: Path) -> Path:
    return approvals_dir / _QUEUE


def _path(approvals_dir: Path, approval_id: str) -> Path:
    return _queue_dir(approvals_dir) / f"{approval_id}.json"


def _gen_code() -> str:
    """A short, human-typable, unambiguous code (6 uppercase alphanumerics)."""
    return new_id("ap").split("_", 1)[1][:6].upper()


def request_approval(
    approvals_dir: Path, *, agent_id: str, task_id: str, action: str, reason: str | None = None,
    channel: str | None = None, conversation_id: Any = None,
) -> dict[str, Any]:
    ensure_dir(_queue_dir(approvals_dir))
    record = {
        "id": new_id("approval"), "code": _gen_code(), "status": "pending",
        "agent_id": agent_id, "task_id": task_id, "action": action, "reason": reason,
        "channel": channel, "conversation_id": conversation_id,
        "created_at": now_iso(), "resolved_at": None, "consumed": False,
    }
    write_json(_path(approvals_dir, record["id"]), record)
    return record


def _all(approvals_dir: Path) -> list[dict[str, Any]]:
    queue = _queue_dir(approvals_dir)
    if not queue.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(queue.glob("*.json")):
        try:
            records.append(read_json(path))
        except (OSError, ValueError):
            continue
    return records


def pending_approvals(approvals_dir: Path) -> list[dict[str, Any]]:
    return [a for a in _all(approvals_dir) if a.get("status") == "pending"]


def find_by_code(approvals_dir: Path, code: str) -> dict[str, Any] | None:
    code = (code or "").strip().upper()
    return next(
        (a for a in _all(approvals_dir) if str(a.get("code", "")).upper() == code and a.get("status") == "pending"),
        None,
    )


def resolve(approvals_dir: Path, code: str, *, approved: bool, note: str | None = None) -> dict[str, Any] | None:
    record = find_by_code(approvals_dir, code)
    if record is None:
        return None
    record["status"] = "approved" if approved else "denied"
    record["resolved_at"] = now_iso()
    if note:
        record["note"] = note
    write_json(_path(approvals_dir, record["id"]), record)
    return record


def resolve_and_requeue(
    approvals_dir: Path, tasks_dir: Path, code: str, *, approved: bool, note: str | None = None
) -> dict[str, Any] | None:
    """Resolve an approval; on approve, re-queue the held task (→ pending) so the
    agent re-runs and the gate lets the approved action through."""
    record = resolve(approvals_dir, code, approved=approved, note=note)
    if record is None:
        return None
    if approved and record.get("task_id"):
        try:
            set_task_state(tasks_dir, record["task_id"], "pending")
        except (ValueError, OSError):
            pass  # task may have been cleaned up; the approval record still stands
    return record


def consume_if_approved(approvals_dir: Path, *, task_id: str, action: str) -> bool:
    """If an approved, unconsumed approval exists for (task_id, action), mark it
    consumed and return True — the gate lets the action through exactly once."""
    for record in _all(approvals_dir):
        if (
            record.get("status") == "approved"
            and not record.get("consumed")
            and record.get("task_id") == task_id
            and record.get("action") == action
        ):
            record["consumed"] = True
            write_json(_path(approvals_dir, record["id"]), record)
            return True
    return False


def parse_approval_reply(text: str | None) -> tuple[bool, str] | None:
    """Parse `approve <code>` / `deny <code>` → (approved, code), else None."""
    match = _REPLY.match(text or "")
    if not match:
        return None
    return (match.group(1).lower() in _APPROVE_WORDS, match.group(2))

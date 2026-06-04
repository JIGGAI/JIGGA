"""File-backed agent mailbox (Teams & Shared Workspaces, slice W6 / #62).

The free-form half of agent coordination, complementing the structured handoff
decision log (H3). Durable, greppable inbox messages — agent→agent and
human→agent — as files, so they show up in `jigga trace`, survive restarts,
and are searchable via the memory index. No ephemeral bus (the file-first
coordination invariant).

Layout (one JSON file per message, append-only):

    workspaces/<team>/roles/<member>/inbox/<msg_id>.json

A message is `{id, from, to, subject?, body, created_at, read_at?}`.
Mark-read annotates `read_at` in place — never moves or deletes, so the inbox
remains a complete, auditable correspondence record.

Delivery to the recipient: unread messages are surfaced in the recipient's
context pack on wake (a volatile, private layer); the runtime marks them read
after a successful run (`mark_read` from `agent.py`), so a failed run re-sees
them next wake.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, read_json, write_json
from jigga.core.models import now_iso
from jigga.runtime.audit import new_id
from jigga.runtime.workspaces import workspace_dir

_MAX_BODY_CHARS = 4000


def inbox_dir(home: Path, team_id: str, member: str) -> Path:
    return workspace_dir(home, team_id) / "roles" / member / "inbox"


def send_message(home: Path, team_id: str, to: str, body: str, *,
                 sender: str, subject: str | None = None) -> dict[str, Any]:
    """Append a message file to `to`'s inbox in the team workspace. Returns the
    message dict. Body is bounded so one send can't flood a context pack."""
    if not to or not str(to).strip():
        raise ValueError("mailbox.send requires a 'to' member id")
    if not body or not str(body).strip():
        raise ValueError("mailbox.send requires a non-empty 'body'")
    message = {
        "id": new_id("msg"),
        "from": str(sender),
        "to": str(to),
        "subject": str(subject) if subject else None,
        "body": str(body)[:_MAX_BODY_CHARS],
        "created_at": now_iso(),
        "read_at": None,
    }
    directory = inbox_dir(home, team_id, str(to))
    ensure_dir(directory)
    write_json(directory / f"{message['id']}.json", message)
    return message


def list_messages(home: Path, team_id: str, member: str, *,
                  unread_only: bool = False) -> list[dict[str, Any]]:
    """Messages in a member's inbox, oldest first."""
    directory = inbox_dir(home, team_id, member)
    if not directory.exists():
        return []
    messages: list[dict[str, Any]] = []
    for path in sorted(directory.glob("msg_*.json")):
        try:
            message = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict):
            continue
        if unread_only and message.get("read_at"):
            continue
        messages.append(message)
    messages.sort(key=lambda m: str(m.get("created_at") or ""))
    return messages


def unread_messages(home: Path, team_id: str, member: str) -> list[dict[str, Any]]:
    return list_messages(home, team_id, member, unread_only=True)


def mark_read(home: Path, team_id: str, member: str, message_ids: list[str]) -> int:
    """Annotate `read_at` on the given messages (in place — never moved or
    deleted). Returns how many were newly marked."""
    directory = inbox_dir(home, team_id, member)
    marked = 0
    for message_id in message_ids:
        path = directory / f"{message_id}.json"
        if not path.exists():
            continue
        try:
            message = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict) or message.get("read_at"):
            continue
        message["read_at"] = now_iso()
        write_json(path, message)
        marked += 1
    return marked


def render_unread(messages: list[dict[str, Any]], *, limit: int = 5) -> str:
    """Unread messages as a context-pack block (oldest first, bounded)."""
    if not messages:
        return ""
    shown = messages[:limit]
    lines = [f"You have {len(messages)} unread message(s) — they'll be marked read "
             "after this run; act on them or note them in your MEMORY.md:"]
    for message in shown:
        subject = f" — {message['subject']}" if message.get("subject") else ""
        lines.append(f"### From `{message.get('from', '?')}`{subject} ({str(message.get('created_at') or '')[:16]})\n"
                     f"{message.get('body', '')}")
    if len(messages) > limit:
        lines.append(f"…and {len(messages) - limit} more — see your inbox/ folder.")
    return "\n\n".join(lines)

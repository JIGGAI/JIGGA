"""Team & role memory write pipelines (Milestone D, slice D2).

Durable, reusable knowledge for a team lives in its workspace — distinct from
`agent-outputs/` (deliverables); this is what's worth *reusing* later:

  shared-context/memory/team.jsonl   — append-only stream of facts / runbooks / decisions
  shared-context/memory/pinned.jsonl — curated high-signal subset (escalated from team.jsonl)
  roles/<member>/MEMORY.md           — per-role continuity

Agents persist here via the `memory.remember` capability; `memory.search` (D1)
indexes it so it's retrievable. Mirrors the ClawRecipes layered memory model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.io import append_jsonl, ensure_dir, read_jsonl
from jigga.core.models import now_iso
from jigga.runtime.audit import new_id
from jigga.runtime.workspaces import workspace_dir


def team_memory_path(home: Path, team_id: str) -> Path:
    return workspace_dir(home, team_id) / "shared-context" / "memory" / "team.jsonl"


def pinned_path(home: Path, team_id: str) -> Path:
    return workspace_dir(home, team_id) / "shared-context" / "memory" / "pinned.jsonl"


def role_memory_path(home: Path, team_id: str, member: str) -> Path:
    return workspace_dir(home, team_id) / "roles" / member / "MEMORY.md"


def append_team_memory(
    home: Path, team_id: str, *, text: str, type: str = "fact",
    tags: list[str] | None = None, source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a durable knowledge entry to the team's `team.jsonl`."""
    entry = {"id": new_id("mem"), "time": now_iso(), "type": type, "text": text,
             "tags": list(tags or []), "source": source or {}}
    append_jsonl(team_memory_path(home, team_id), entry)
    return entry


def read_team_memory(home: Path, team_id: str) -> list[dict[str, Any]]:
    return read_jsonl(team_memory_path(home, team_id))


def read_pinned(home: Path, team_id: str) -> list[dict[str, Any]]:
    return read_jsonl(pinned_path(home, team_id))


def pin_entry(home: Path, team_id: str, entry_id: str) -> dict[str, Any] | None:
    """Escalate an entry from team.jsonl into pinned.jsonl (high-signal). Matches
    by exact id or id prefix."""
    for entry in read_team_memory(home, team_id):
        eid = str(entry.get("id", ""))
        if eid == entry_id or eid.startswith(entry_id):
            append_jsonl(pinned_path(home, team_id), {**entry, "pinned_at": now_iso()})
            return entry
    return None


def append_role_memory(home: Path, team_id: str, member: str, text: str) -> Path:
    """Append a dated note to a role's `MEMORY.md` (per-role continuity)."""
    path = role_memory_path(home, team_id, member)
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {now_iso()}\n{text}\n")
    return path

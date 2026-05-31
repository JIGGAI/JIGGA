"""Per-team shared workspaces with a file-first curator model (Milestone:
Teams & Shared Workspaces, slice W1).

Adopts the ClawRecipes team model into JIGGA: each team gets a workspace under
`~/.jigga/workspaces/<team>/` that its members coordinate through — files, not
DMs. The **lead** curates `plan.md` / `priorities.md`; other members **append**
to `agent-outputs/` and `feedback/` (the read → act → write loop). Ticket lanes
(W3) and team/role memory `.jsonl` (W2) layer on later.

Layout:
    workspaces/<team>/
      TEAM.md                        # name / purpose / members / lead
      notes/plan.md                  # lead-curated (create-only)
      notes/status.md                # append-only operational log
      shared-context/priorities.md   # lead-curated (create-only)
      shared-context/agent-outputs/  # append-only, per-member outputs
      shared-context/feedback/       # append-only QA / feedback
      roles/<member>/                # per-member space
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir
from jigga.core.models import TeamConfig, now_iso

# Files only the lead may edit; everyone else appends to agent-outputs/feedback.
CURATED = ("notes/plan.md", "shared-context/priorities.md")


class CuratorError(PermissionError):
    """Raised when a non-lead member tries to write a lead-curated file."""


def workspaces_root(home: Path) -> Path:
    return Path(home) / "workspaces"


def workspace_dir(home: Path, team_id: str) -> Path:
    return workspaces_root(home) / team_id


def members(team: TeamConfig) -> list[str]:
    return [str(a.get("id")) for a in (team.agents or []) if a.get("id")]


def team_lead(team: TeamConfig) -> str | None:
    """The curator. Prefer `routing.default_assignee`; else the first member."""
    lead = (team.routing or {}).get("default_assignee")
    if lead:
        return str(lead)
    found = members(team)
    return found[0] if found else None


def is_curated(relpath: str) -> bool:
    return relpath.replace("\\", "/") in CURATED


def _write_if_absent(path: Path, content: str) -> bool:
    if path.exists():
        return False
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")
    return True


def scaffold_workspace(home: Path, team: TeamConfig) -> dict[str, Any]:
    """Create the workspace layout + create-only starter files. Idempotent —
    existing files are never overwritten (re-running is safe)."""
    root = workspace_dir(home, team.id)
    member_ids = members(team)
    lead = team_lead(team)
    for sub in ("notes", "shared-context/agent-outputs", "shared-context/feedback"):
        ensure_dir(root / sub)
    for member in member_ids:
        ensure_dir(root / "roles" / member)

    roster = "".join(f"- {m}{'  (lead/curator)' if m == lead else ''}\n" for m in member_ids)
    starters = {
        "TEAM.md": f"# {team.name}\n\n{team.purpose or ''}\n\n## Members\n{roster}",
        "notes/plan.md": (
            f"# Plan — {team.name}\n\n_Lead-curated ({lead or 'unassigned'}). Other members: "
            "discuss via shared-context/agent-outputs or feedback — don't edit this file._\n"
        ),
        "notes/status.md": f"# Status — {team.name}\n\n_Append-only operational log (short, frequent)._\n",
        "shared-context/priorities.md": (
            f"# Priorities — {team.name}\n\n_Lead-curated ({lead or 'unassigned'})._\n"
        ),
    }
    created = [rel for rel, content in starters.items() if _write_if_absent(root / rel, content)]
    return {"workspace": str(root), "members": member_ids, "lead": lead, "created": created}


def read_file(home: Path, team_id: str, relpath: str) -> str | None:
    path = workspace_dir(home, team_id) / relpath
    return path.read_text(encoding="utf-8") if path.exists() else None


def append_status(home: Path, team_id: str, note: str) -> Path:
    path = workspace_dir(home, team_id) / "notes" / "status.md"
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n- {now_iso()} {note}")
    return path


def append_agent_output(home: Path, team_id: str, member: str, text: str) -> Path:
    """Append a member's deliverable to its append-only outputs file (any member
    may do this — it's how non-leads contribute without touching curated docs)."""
    path = workspace_dir(home, team_id) / "shared-context" / "agent-outputs" / f"{member}.md"
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## {now_iso()}\n{text}\n")
    return path


def write_curated(home: Path, team: TeamConfig, relpath: str, content: str, *, member: str) -> Path:
    """Lead-only write to a curated file (plan.md / priorities.md). A non-lead
    member gets a CuratorError — they append to agent-outputs/feedback instead."""
    if not is_curated(relpath):
        raise ValueError(f"{relpath!r} is not a curated file ({', '.join(CURATED)})")
    lead = team_lead(team)
    if lead is not None and member != lead:
        raise CuratorError(
            f"{relpath} is lead-curated ({lead}); {member} should append to "
            "shared-context/agent-outputs/ or feedback/ instead."
        )
    path = workspace_dir(home, team.id) / relpath
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")
    return path

"""Memory write-proposal queue (Milestone D, slice D4).

Sensitive memory writes (facts / preferences / relationships about the user)
shouldn't be saved silently. When `memory.require_approval` is on, a
`memory.remember` of a sensitive type is **parked as a proposal** instead of
written; the user reviews the digest and approves/rejects. Approved proposals
commit to the team's `team.jsonl`. Opt-in — off by default, so existing
direct-write behavior is unchanged.

File-first + auditable: pending proposals live in the team workspace at
`shared-context/memory/proposals.jsonl`.

Config:
    memory:
      require_approval: true
      sensitive_types: [fact, preference, relationship]   # default
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_runtime_config
from jigga.core.io import read_jsonl, rewrite_jsonl
from jigga.core.models import now_iso
from jigga.runtime.audit import new_id
from jigga.runtime.team_memory import append_team_memory
from jigga.runtime.workspaces import workspace_dir

_DEFAULT_SENSITIVE = ("fact", "preference", "relationship")


def proposals_path(home: Path, team_id: str) -> Path:
    return workspace_dir(home, team_id) / "shared-context" / "memory" / "proposals.jsonl"


def sensitive_requires_approval(home: Path, mem_type: str) -> bool:
    cfg = load_runtime_config(home).get("memory") or {}
    if not cfg.get("require_approval", False):
        return False
    sensitive = cfg.get("sensitive_types") or _DEFAULT_SENSITIVE
    return mem_type in set(sensitive)


def propose(home: Path, team_id: str, *, text: str, type: str = "fact",
            tags: list[str] | None = None, source: dict[str, Any] | None = None) -> dict[str, Any]:
    entry = {"id": new_id("prop"), "time": now_iso(), "type": type, "text": text,
             "tags": list(tags or []), "source": source or {}, "status": "pending"}
    path = proposals_path(home, team_id)
    rewrite_jsonl(path, [*read_jsonl(path), entry])
    return entry


def _team_ids(home: Path, team_id: str | None) -> list[str]:
    if team_id:
        return [team_id]
    workspaces = Path(home) / "workspaces"
    return sorted(p.name for p in workspaces.iterdir() if p.is_dir()) if workspaces.exists() else []


def list_proposals(home: Path, team_id: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tid in _team_ids(home, team_id):
        for entry in read_jsonl(proposals_path(home, tid)):
            if entry.get("status") == "pending":
                out.append({**entry, "team": tid})
    return out


def apply_proposal(home: Path, proposal_id: str, *, approve: bool, team_id: str | None = None) -> dict[str, Any] | None:
    """Approve (commit to team.jsonl) or reject a pending proposal, by exact id
    or prefix. Removes it from the pending queue. Returns the resolved entry."""
    for tid in _team_ids(home, team_id):
        path = proposals_path(home, tid)
        pending = read_jsonl(path)
        match = next((e for e in pending
                      if e.get("status") == "pending"
                      and (str(e.get("id", "")) == proposal_id or str(e.get("id", "")).startswith(proposal_id))), None)
        if match is None:
            continue
        rewrite_jsonl(path, [e for e in pending if e is not match])
        if approve:
            committed = append_team_memory(home, tid, text=match["text"], type=match.get("type", "fact"),
                                           tags=match.get("tags"), source=match.get("source"))
            return {**match, "team": tid, "status": "approved", "memory_id": committed["id"]}
        return {**match, "team": tid, "status": "rejected"}
    return None

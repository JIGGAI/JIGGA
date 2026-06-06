"""Workspace file surfaces for agents and teams (jiggaview's file editors).

ClawKitchen's agent/team Files tabs list a file set (required + optional,
missing flagged) and edit one file at a time. JIGGA's equivalents live in the
entity's workspace:

  agent files:  workspaces/<ws>/roles/<agent>/  (SOUL/AGENTS/MEMORY required —
                the identity-file minimum — TOOLS/USER optional notes)
  team files:   workspaces/<team>/              (TEAM.md, lead-curated plan and
                priorities, status log; roles/ files belong to the agents)

Reads/writes are workspace-confined (no traversal or absolute-path escape) and
every write is audited (`workspace.file_edited`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.runtime.audit import append_event
from jigga.runtime.workspaces import workspace_dir

AGENT_FILES: list[tuple[str, bool]] = [
    ("SOUL.md", True),      # persona — who the agent is
    ("AGENTS.md", True),    # charter + guardrails
    ("MEMORY.md", True),    # the agent's own curated notes
    ("TOOLS.md", False),    # optional usage notes (grants stay in yaml)
    ("USER.md", False),     # optional per-agent principal override
]

TEAM_FILES: list[tuple[str, bool]] = [
    ("TEAM.md", True),
    ("notes/plan.md", True),
    ("notes/status.md", True),
    ("shared-context/priorities.md", True),
]


def _confined(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    root = root.resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"Path {name!r} escapes the workspace")
    return target


def _listing(root: Path, manifest: list[tuple[str, bool]]) -> list[dict[str, Any]]:
    return [
        {"name": name, "required": required, "missing": not (root / name).exists()}
        for name, required in manifest
    ]


def agent_files_root(home: Path, workspace_id: str, agent_id: str) -> Path:
    return workspace_dir(home, workspace_id) / "roles" / agent_id


def list_agent_files(home: Path, workspace_id: str, agent_id: str) -> list[dict[str, Any]]:
    return _listing(agent_files_root(home, workspace_id, agent_id), AGENT_FILES)


def list_team_files(home: Path, team_id: str) -> list[dict[str, Any]]:
    return _listing(workspace_dir(home, team_id), TEAM_FILES)


def read_entity_file(root: Path, name: str) -> str | None:
    target = _confined(root, name)
    return target.read_text(encoding="utf-8") if target.exists() else None


def write_entity_file(root: Path, name: str, content: str, *, logs_dir: Path,
                      entity: str) -> Path:
    target = _confined(root, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    append_event(logs_dir, "workspace.file_edited", entity=entity, file=name,
                 bytes=len(content.encode("utf-8")))
    return target

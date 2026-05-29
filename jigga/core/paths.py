from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JiggaPaths:
    home: Path
    config: Path
    state: Path
    agents: Path
    teams: Path
    workflows: Path
    tasks: Path
    capabilities: Path
    memory: Path
    logs: Path
    policies: Path
    approvals: Path
    runs: Path
    sessions: Path


def resolve_home(home: str | Path | None = None) -> Path:
    raw = home or os.environ.get("JIGGA_HOME") or Path.home() / ".jigga"
    return Path(raw).expanduser().resolve()


def get_paths(home: str | Path | None = None) -> JiggaPaths:
    root = resolve_home(home)
    return JiggaPaths(
        home=root,
        config=root / "config.yaml",
        state=root / "state.json",
        agents=root / "agents",
        teams=root / "teams",
        workflows=root / "workflows",
        tasks=root / "tasks",
        capabilities=root / "capabilities",
        memory=root / "memory",
        logs=root / "logs",
        policies=root / "policies",
        approvals=root / "approvals",
        runs=root / "runs",
        sessions=root / "sessions",
    )


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def examples_dir() -> Path:
    return repo_root() / "examples"


def find_project_root(start: str | Path | None = None) -> Path | None:
    """Walk upward from `start` (or cwd) looking for a directory containing a
    `.jigga/` subdirectory. Returns the parent of that `.jigga/` (the project
    root), or None if no such directory is found before reaching the
    filesystem root.

    Mirrors the gitignore-style auto-discovery pattern: a workspace becomes
    "JIGGA-aware" by creating a `.jigga/` directory inside it. Auto-detection
    is best-effort — explicit `--project <dir>` (or `JIGGA_PROJECT` env var)
    always overrides this.
    """
    raw = start or Path.cwd()
    current = Path(raw).expanduser().resolve()
    # If `start` is a file, scan from its parent.
    if current.is_file():
        current = current.parent
    while True:
        if (current / ".jigga").is_dir():
            return current
        if current.parent == current:
            return None
        current = current.parent


def resolve_project_root(project: str | Path | None = None) -> Path | None:
    """Resolution order for the project root:
    1. Explicit `project` argument (e.g. CLI `--project <path>`).
    2. `JIGGA_PROJECT` environment variable.
    3. Auto-detect via `find_project_root()` from cwd.
    4. None — caller treats project capabilities as unavailable.
    """
    explicit = project or os.environ.get("JIGGA_PROJECT")
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        return candidate if candidate.is_dir() else None
    return find_project_root()


def project_capabilities_dir(project_root: str | Path | None) -> Path | None:
    """Return `<project_root>/.jigga/capabilities` if the project root resolves
    to an existing directory; None otherwise. Used by the CLI and workflow
    runner to construct the optional `project_capabilities` arg passed to
    `CapabilityRegistry.load`.
    """
    if project_root is None:
        return None
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        return None
    # Return the path regardless of whether it exists — scan_capability_dir
    # handles missing directories by returning an empty list, which keeps the
    # "no project capabilities yet" case from being special-cased downstream.
    return root / ".jigga" / "capabilities"

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

from __future__ import annotations

from pathlib import Path

from jigga.core.io import list_config_files, read_yaml
from jigga.core.models import AgentConfig, MemoryScope, TeamConfig, WorkflowConfig, validate_permission_mode

DEFAULT_MAX_WAKES_PER_HOUR = 12
DEFAULT_PERMISSION_MODE = "ask"
# How long one supervisor tick may spend waking agents before it stops starting
# new ones. The supervisor is a single sequential process, so a slow or hung
# agent delays every other agent behind it — this bounds that blast radius.
# Deferred agents keep their pending tasks and run on the next tick.
DEFAULT_MAX_TICK_SECONDS = 300
# Default wall-clock ceiling for a single capability invocation. A capability
# may raise its own via `limits.timeout_seconds` in its manifest.
DEFAULT_CAPABILITY_TIMEOUT_SECONDS = 120


def load_runtime_config(home: Path) -> dict:
    config_file = home / "config.yaml"
    if not config_file.exists():
        return {}
    return read_yaml(config_file) or {}


def max_wakes_per_hour(home: Path) -> int:
    config = load_runtime_config(home)
    supervisor = config.get("supervisor") or {}
    return int(supervisor.get("max_wakes_per_agent_per_hour", DEFAULT_MAX_WAKES_PER_HOUR))


def max_tick_seconds(home: Path) -> float:
    """Wall-clock budget for the agent-waking phase of one tick (0 = unbounded)."""
    config = load_runtime_config(home)
    supervisor = config.get("supervisor") or {}
    try:
        return max(0.0, float(supervisor.get("max_tick_seconds", DEFAULT_MAX_TICK_SECONDS)))
    except (TypeError, ValueError):
        return float(DEFAULT_MAX_TICK_SECONDS)


def default_capability_timeout(home: Path) -> float:
    """Default per-invocation ceiling for capabilities (0 = unbounded)."""
    config = load_runtime_config(home)
    capabilities = config.get("capabilities") or {}
    try:
        return max(0.0, float(capabilities.get("default_timeout_seconds",
                                               DEFAULT_CAPABILITY_TIMEOUT_SECONDS)))
    except (TypeError, ValueError):
        return float(DEFAULT_CAPABILITY_TIMEOUT_SECONDS)


def default_permission_mode(home: Path) -> str:
    config = load_runtime_config(home)
    defaults = config.get("defaults") or {}
    mode = defaults.get("permission_mode", DEFAULT_PERMISSION_MODE)
    return validate_permission_mode(str(mode))


def load_agents(path: Path) -> dict[str, AgentConfig]:
    agents: dict[str, AgentConfig] = {}
    for file in list_config_files(path):
        agent = AgentConfig.from_dict(read_yaml(file), source=str(file))
        agents[agent.id] = agent
    return agents


def resolve_default_agent(agents_dir: Path) -> str | None:
    """The id of the default/primary agent (chief of staff / personal assistant)
    — the entry with `default: true`. Ties break by sorted id for determinism.
    None when no agent is marked default."""
    defaults = sorted(a.id for a in load_agents(agents_dir).values() if getattr(a, "default", False))
    return defaults[0] if defaults else None


def load_teams(path: Path) -> dict[str, TeamConfig]:
    teams: dict[str, TeamConfig] = {}
    for file in list_config_files(path):
        team = TeamConfig.from_dict(read_yaml(file), source=str(file))
        teams[team.id] = team
    return teams


def load_workflows(path: Path) -> dict[str, WorkflowConfig]:
    workflows: dict[str, WorkflowConfig] = {}
    for file in list_config_files(path):
        workflow = WorkflowConfig.from_dict(read_yaml(file), source=str(file))
        workflows[workflow.id] = workflow
    return workflows


def load_memory_scopes(memory_path: Path) -> dict[str, MemoryScope]:
    scopes: dict[str, MemoryScope] = {}
    scope_file = memory_path / "memory_scopes.yaml"
    if not scope_file.exists():
        return scopes
    data = read_yaml(scope_file).get("memory_scopes", {})
    for scope_id, scope_data in data.items():
        scopes[scope_id] = MemoryScope.from_dict(scope_id, scope_data or {})
    return scopes

from __future__ import annotations

from pathlib import Path

from jigga.core.io import list_config_files, read_yaml
from jigga.core.models import AgentConfig, MemoryScope, TeamConfig, WorkflowConfig, validate_permission_mode

DEFAULT_MAX_WAKES_PER_HOUR = 12
DEFAULT_PERMISSION_MODE = "ask"


def load_runtime_config(home: Path) -> dict:
    config_file = home / "config.yaml"
    if not config_file.exists():
        return {}
    return read_yaml(config_file) or {}


def max_wakes_per_hour(home: Path) -> int:
    config = load_runtime_config(home)
    supervisor = config.get("supervisor") or {}
    return int(supervisor.get("max_wakes_per_agent_per_hour", DEFAULT_MAX_WAKES_PER_HOUR))


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

from __future__ import annotations

from pathlib import Path

from jigga.core.io import list_config_files, read_yaml
from jigga.core.models import AgentConfig, MemoryScope, TeamConfig, WorkflowConfig


def load_agents(path: Path) -> dict[str, AgentConfig]:
    agents: dict[str, AgentConfig] = {}
    for file in list_config_files(path):
        agent = AgentConfig.from_dict(read_yaml(file), source=str(file))
        agents[agent.id] = agent
    return agents


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

from __future__ import annotations

from pathlib import Path

from jigga.core.io import list_config_files, read_yaml
from jigga.core.models import AgentConfig, TeamConfig


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

"""Disable/enable for agents and teams — reversible operational state.

Stored in config.yaml (`disabled.agents` / `disabled.teams`), NOT in the
entity yamls: disabling is runtime state, not identity, so recipe-managed
yamls stay pristine (no drift) and re-enabling is one config flip.

Semantics: the supervisor never wakes a disabled agent — cron schedules are
skipped, pending tasks stay pending (visible, never lost), mail wakes are
withheld, and channel ingest won't run it. A disabled TEAM disables all its
roster members. Manual `jigga run agent` still works (explicit human intent
overrides), with a notice.
"""

from __future__ import annotations

from pathlib import Path

from jigga.core.config import load_runtime_config, load_teams


def disabled_sets(home: Path) -> tuple[set[str], set[str]]:
    """(disabled agent ids, disabled team ids) from config."""
    config = load_runtime_config(Path(home))
    disabled = config.get("disabled") or {}
    if not isinstance(disabled, dict):
        return set(), set()
    agents = {str(a) for a in (disabled.get("agents") or [])}
    teams = {str(t) for t in (disabled.get("teams") or [])}
    return agents, teams


def disabled_agent_ids(home: Path, teams_dir: Path) -> set[str]:
    """Every agent the supervisor must not wake: directly disabled agents plus
    all roster members of disabled teams."""
    agents, teams = disabled_sets(home)
    if teams:
        for team in load_teams(Path(teams_dir)).values():
            if team.id in teams:
                for member in team.agents or []:
                    if isinstance(member, dict) and member.get("id"):
                        agents.add(str(member["id"]))
    return agents

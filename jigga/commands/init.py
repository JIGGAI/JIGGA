from __future__ import annotations

from pathlib import Path

from jigga.core.io import copy_if_missing, ensure_dir, write_json, write_yaml
from jigga.core.models import State
from jigga.core.paths import examples_dir, get_paths


def init_runtime(home: str | Path | None = None, examples: bool = False):
    paths = get_paths(home)
    for directory in [
        paths.home,
        paths.agents,
        paths.teams,
        paths.workflows,
        paths.tasks,
        paths.capabilities,
        paths.memory / "raw",
        paths.memory / "structured",
        paths.memory / "summaries",
        paths.memory / "indexes",
        paths.logs,
        paths.policies,
        paths.approvals,
        paths.runs,
        paths.sessions,
    ]:
        ensure_dir(directory)

    if not paths.config.exists():
        write_yaml(
            paths.config,
            {
                "version": 1,
                "home": str(paths.home),
                "supervisor": {"interval_seconds": 60, "max_wakes_per_agent_per_hour": 12},
                "defaults": {"memory_scope": "task_only", "permission_mode": "ask"},
                "delegation_policy": {
                    "enabled": True,
                    "allowed_backends": ["dry_run"],
                    "codex_cli_enabled": False,
                    "max_global_subagents": 8,
                    "max_subagents_per_parent": 4,
                    "max_depth": 1,
                    "default_runtime_minutes": 20,
                },
                "models": {
                    "defaults": {"provider": "dry_run"},
                    "providers": {
                        "dry_run": {"kind": "dry_run", "default_model": "dry-run"},
                    },
                    "profiles": {
                        "default": {"primary": "dry_run", "fallback": []},
                    },
                },
            },
        )

    if not paths.state.exists():
        write_json(paths.state, State().to_dict())

    if examples:
        ex = examples_dir()
        for source, target in [
            (ex / "agents" / "daily_briefing_agent.yaml", paths.agents / "daily_briefing_agent.yaml"),
            (ex / "agents" / "content_strategist.yaml", paths.agents / "content_strategist.yaml"),
            (ex / "teams" / "personal_admin_team.yaml", paths.teams / "personal_admin_team.yaml"),
            (ex / "teams" / "social_content_team.yaml", paths.teams / "social_content_team.yaml"),
            (ex / "workflows" / "morning_day_summary.yaml", paths.workflows / "morning_day_summary.yaml"),
            (ex / "workflows" / "meeting_reminders.yaml", paths.workflows / "meeting_reminders.yaml"),
            (ex / "workflows" / "social_content_syndication.yaml", paths.workflows / "social_content_syndication.yaml"),
            (ex / "memory" / "memory_scopes.yaml", paths.memory / "memory_scopes.yaml"),
        ]:
            copy_if_missing(source, target)

    return paths

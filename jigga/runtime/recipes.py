"""Recipe-driven team scaffolding (Teams & Shared Workspaces, slice W4).

ClawRecipes-style: a **Markdown recipe** (YAML frontmatter + free-form body)
describes a team and its member roles; `scaffold_team` generates the JIGGA agent
+ team YAML definitions (`<teamId>-<role>`), templates `{{teamId}}` /
`{{teamName}}`, and scaffolds the shared workspace (reusing W1). This is what
`jigga init --examples` (file copy) and `jigga team init` (workspace only) are
not — actual generation of new agents/teams from a template.

Recipe shape (frontmatter):
    id / name / kind: team / version / description / purpose
    routing: {lead: <role>}
    agents:
      - role: lead
        name: "{{teamName}} Lead"
        description: ...
        tools: [draft_with_model]
        model: profile:default        # optional
        permissions: {...}            # optional

Deferred to follow-ups: cronJobs (schedules), agentTools policy, arbitrary
files/templates, and `kind: agent` single-agent recipes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jigga.core.io import ensure_dir, write_yaml
from jigga.core.models import TeamConfig
from jigga.core.paths import examples_dir
from jigga.runtime.workspaces import scaffold_workspace

RECIPE_SUFFIX = ".md"


@dataclass
class Recipe:
    id: str
    name: str
    kind: str = "team"
    agents: list[dict[str, Any]] = field(default_factory=list)
    purpose: str | None = None
    description: str | None = None
    version: str | None = None
    routing: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    source: str | None = None


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.lstrip().startswith("---"):
        return {}, text
    parts = text.lstrip().split("---", 2)  # ["", <yaml>, <body>]
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid recipe frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError("Recipe frontmatter must be a YAML mapping")
    return meta, parts[2].strip()


def load_recipe(path: Path) -> Recipe:
    meta, body = _parse_frontmatter(Path(path).read_text(encoding="utf-8"))
    if not meta.get("id") or not meta.get("name"):
        raise ValueError(f"Recipe {path} is missing id/name in frontmatter")
    return Recipe(
        id=str(meta["id"]), name=str(meta["name"]), kind=str(meta.get("kind", "team")),
        agents=list(meta.get("agents") or []), purpose=meta.get("purpose"),
        description=meta.get("description"), version=meta.get("version"),
        routing=dict(meta.get("routing") or {}), body=body, source=str(path),
    )


def recipes_dirs(home: Path) -> list[Path]:
    """User recipes first, then bundled examples."""
    return [Path(home) / "recipes", examples_dir() / "recipes"]


def find_recipe(home: Path, name_or_path: str) -> Path | None:
    direct = Path(name_or_path)
    if direct.exists():
        return direct
    filename = name_or_path if name_or_path.endswith(RECIPE_SUFFIX) else name_or_path + RECIPE_SUFFIX
    for directory in recipes_dirs(home):
        candidate = directory / filename
        if candidate.exists():
            return candidate
    return None


def list_recipes(home: Path) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for directory in recipes_dirs(home):
        if not directory.exists():
            continue
        for path in sorted(directory.glob(f"*{RECIPE_SUFFIX}")):
            try:
                recipe = load_recipe(path)
            except ValueError:
                continue
            found.setdefault(recipe.id, {"id": recipe.id, "name": recipe.name, "kind": recipe.kind,
                                         "description": recipe.description, "source": str(path)})
    return list(found.values())


def _template(value: Any, ctx: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, replacement in ctx.items():
            value = value.replace("{{" + key + "}}", replacement)
        return value
    if isinstance(value, list):
        return [_template(item, ctx) for item in value]
    if isinstance(value, dict):
        return {key: _template(item, ctx) for key, item in value.items()}
    return value


def scaffold_team(
    home: Path, recipe: Recipe, *, team_id: str | None = None, overwrite: bool = False,
    agents_dir: Path | None = None, teams_dir: Path | None = None,
) -> dict[str, Any]:
    """Generate `<teamId>-<role>` agent YAMLs + a team YAML from a recipe, then
    scaffold the workspace. Existing files are skipped unless `overwrite`."""
    if recipe.kind != "team":
        raise ValueError(f"scaffold_team requires kind: team (got {recipe.kind!r})")
    home = Path(home)
    team_id = team_id or recipe.id
    agents_dir = agents_dir or home / "agents"
    teams_dir = teams_dir or home / "teams"
    ensure_dir(agents_dir)
    ensure_dir(teams_dir)
    ctx = {"teamId": team_id, "teamName": recipe.name}

    lead_role = recipe.routing.get("lead") or (recipe.agents[0].get("role") if recipe.agents else None)
    members: list[dict[str, Any]] = []
    written: list[str] = []
    skipped: list[str] = []
    for spec in recipe.agents:
        role = str(spec.get("role") or spec.get("id"))
        agent_id = f"{team_id}-{role}"
        agent_doc = {
            "id": agent_id,
            "name": _template(spec.get("name") or role.title(), ctx),
            "role": _template(spec.get("description") or spec.get("role") or role, ctx),
            "memory_scope": "task_only",
            "model": spec.get("model") or "profile:default",
            "tools": _template(list(spec.get("tools") or []), ctx),
            "permissions": dict(spec.get("permissions") or {"network": {"mode": "ask"}, "shell": {"mode": "deny"}}),
        }
        path = agents_dir / f"{agent_id}.yaml"
        if path.exists() and not overwrite:
            skipped.append(agent_id)
        else:
            write_yaml(path, agent_doc)
            written.append(agent_id)
        members.append({"id": agent_id, "role": role, "required": True})

    lead_id = f"{team_id}-{lead_role}" if lead_role else (members[0]["id"] if members else None)
    team_doc = {
        "id": team_id,
        "name": _template(recipe.name, ctx),
        "purpose": _template(recipe.purpose, ctx) if recipe.purpose else None,
        "agents": members,
        "routing": {"default_assignee": lead_id},
    }
    team_path = teams_dir / f"{team_id}.yaml"
    team_written = overwrite or not team_path.exists()
    if team_written:
        write_yaml(team_path, team_doc)

    workspace = scaffold_workspace(home, TeamConfig.from_dict(team_doc))
    return {
        "team_id": team_id, "team_file": str(team_path), "team_written": team_written,
        "agents_written": written, "agents_skipped": skipped, "lead": lead_id,
        "workspace": workspace["workspace"],
    }

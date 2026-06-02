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

Now also supported: `cronJobs` (scheduled work-loops → agent `wake.schedules`),
per-role `permissions`/`tools`, `kind: agent` single-agent recipes, and
`files:`/`templates:` (extra workspace files written at scaffold time, templated,
create-only by default). This completes the W4 recipe surface.
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
    meta: dict[str, Any] = field(default_factory=dict)  # full frontmatter (for kind: agent fields, cronJobs, ...)


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
        routing=dict(meta.get("routing") or {}), body=body, source=str(path), meta=meta,
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


def _write_recipe_files(workspace_root: Path, recipe: Recipe, ctx: dict[str, str], *, overwrite: bool) -> dict[str, list[str]]:
    """Write a recipe's `files:` into the workspace. Each entry: `{path,
    content | template, mode: createOnly|overwrite}`; `template` names an entry
    in the recipe's top-level `templates:` map. Content is `{{...}}`-templated.
    Create-only by default. Paths are confined to the workspace (no traversal /
    absolute-path escape)."""
    templates = recipe.meta.get("templates") or {}
    written: list[str] = []
    skipped: list[str] = []
    root = workspace_root.resolve()
    for spec in recipe.meta.get("files") or []:
        if not isinstance(spec, dict) or not spec.get("path"):
            continue
        rel = str(spec["path"])
        target = (workspace_root / rel).resolve()
        if target != root and root not in target.parents:  # escapes the workspace → refuse
            skipped.append(rel)
            continue
        raw = spec.get("content")
        if raw is None and spec.get("template"):
            raw = templates.get(str(spec["template"]), "")
        content = _template(str(raw or ""), ctx)
        allow_overwrite = overwrite or str(spec.get("mode", "createOnly")).lower() == "overwrite"
        if target.exists() and not allow_overwrite:
            skipped.append(rel)
            continue
        ensure_dir(target.parent)
        target.write_text(content, encoding="utf-8")
        written.append(rel)
    return {"written": written, "skipped": skipped}


def _wake_from_cronjobs(cronjobs: Any, ctx: dict[str, str]) -> dict[str, Any]:
    """Map a recipe's `cronJobs` to a JIGGA agent `wake.schedules`. Each entry's
    `schedule` (5-field cron) → `cron`; `message` (the work-loop instruction) is
    carried so the supervisor uses it as the scheduled task. `enabledByDefault:
    false` loops are skipped (safe-idle: don't auto-schedule them)."""
    schedules: list[dict[str, Any]] = []
    for job in cronjobs or []:
        if not isinstance(job, dict) or job.get("enabledByDefault") is False:
            continue
        cron = job.get("schedule") or job.get("cron")
        if not cron:
            continue
        entry: dict[str, Any] = {
            "cron": _template(str(cron), ctx),
            "event": _template(str(job.get("id") or job.get("event") or "work-loop"), ctx),
        }
        if job.get("message"):
            entry["message"] = _template(str(job["message"]), ctx)
        schedules.append(entry)
    return {"schedules": schedules} if schedules else {}


def _agent_doc(*, agent_id: str, name: str, role: str, tools: Any, model: Any,
               permissions: Any, cronjobs: Any, ctx: dict[str, str]) -> dict[str, Any]:
    doc = {
        "id": agent_id,
        "name": _template(name, ctx),
        "role": _template(role, ctx),
        "memory_scope": "task_only",
        "model": model or "profile:default",
        "tools": _template(list(tools or []), ctx),
        "permissions": dict(permissions or {"network": {"mode": "ask"}, "shell": {"mode": "deny"}}),
    }
    wake = _wake_from_cronjobs(cronjobs, ctx)
    if wake:
        doc["wake"] = wake
        # Fail fast on a malformed cron in the recipe rather than scaffolding an
        # agent that silently never wakes.
        from jigga.runtime.validation import validate_cron
        for sched in wake.get("schedules", []):
            cron = sched.get("cron")
            err = validate_cron(cron) if cron else None
            if err:
                raise ValueError(f"recipe agent {agent_id!r} has an invalid cron: {err}")
    return doc


def scaffold_agent(
    home: Path, recipe: Recipe, *, agent_id: str | None = None, overwrite: bool = False,
    agents_dir: Path | None = None,
) -> dict[str, Any]:
    """Scaffold a single agent from a `kind: agent` recipe (its top-level
    frontmatter defines the agent). Create-only unless `overwrite`."""
    if recipe.kind != "agent":
        raise ValueError(f"scaffold_agent requires kind: agent (got {recipe.kind!r})")
    home = Path(home)
    agent_id = agent_id or recipe.id
    agents_dir = agents_dir or home / "agents"
    ensure_dir(agents_dir)
    ctx = {"agentId": agent_id, "agentName": recipe.name, "teamId": agent_id, "teamName": recipe.name}
    meta = recipe.meta
    doc = _agent_doc(
        agent_id=agent_id, name=recipe.name,
        role=meta.get("description") or recipe.description or recipe.name,
        tools=meta.get("tools"), model=meta.get("model"), permissions=meta.get("permissions"),
        cronjobs=meta.get("cronJobs"), ctx=ctx,
    )
    path = agents_dir / f"{agent_id}.yaml"
    written = overwrite or not path.exists()
    if written:
        write_yaml(path, doc)
    # A solo agent is its own one-member team → its own workspace (also created
    # on first run); scaffold it now so recipe `files:` have a home.
    solo_team = TeamConfig.from_dict({"id": agent_id, "name": recipe.name,
                                      "agents": [{"id": agent_id, "role": ""}],
                                      "routing": {"default_assignee": agent_id}})
    workspace = scaffold_workspace(home, solo_team)
    files = _write_recipe_files(Path(workspace["workspace"]), recipe, ctx, overwrite=overwrite)
    return {"kind": "agent", "agent_id": agent_id, "agent_file": str(path), "written": written,
            "scheduled": bool(doc.get("wake")), "workspace": workspace["workspace"],
            "files_written": files["written"], "files_skipped": files["skipped"]}


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
        agent_doc = _agent_doc(
            agent_id=agent_id, name=spec.get("name") or role.title(),
            role=spec.get("description") or spec.get("role") or role,
            tools=spec.get("tools"), model=spec.get("model"), permissions=spec.get("permissions"),
            cronjobs=spec.get("cronJobs"), ctx=ctx,
        )
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
    files = _write_recipe_files(Path(workspace["workspace"]), recipe, ctx, overwrite=overwrite)
    return {
        "team_id": team_id, "team_file": str(team_path), "team_written": team_written,
        "agents_written": written, "agents_skipped": skipped, "lead": lead_id,
        "workspace": workspace["workspace"],
        "files_written": files["written"], "files_skipped": files["skipped"],
    }

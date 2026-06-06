"""Recipe-driven team scaffolding (Teams & Shared Workspaces, slice W4).

ClawRecipes-style: a **Markdown recipe** (YAML frontmatter + free-form body)
describes a team and its member roles; `scaffold_team` generates the JIGGA agent
+ team YAML definitions (`<teamId>-<role>`), templates `{{teamId}}` /
`{{teamName}}`, and scaffolds the shared workspace (reusing W1). This is what
`jigga init --examples` (file copy) and `jigga team init` (workspace only) are
not — actual generation of new agents/teams from a template.

Recipe shape (frontmatter) — recipes are THE example/install format
(`jigga recipes list|show|scaffold`):
    id / name / kind: team|agent / version / description / purpose
    memory_scope / default_workflows / policies   # team-yaml passthrough
    routing:                       # team-yaml passthrough (incl. handoffs);
      lead: <role>                 # `lead:` is sugar → routing.default_assignee
    agents:
      - role: strategy             # team-membership role key
        id: marketing_lead         # explicit agent id (default: <teamId>-<role>)
        required: true
        agent: {...}               # full agent-yaml passthrough (every AgentConfig
                                   # field: wake, notifications, delegation, ...)
                                   # + `cronJobs` sugar → wake.schedules
      - role: meeting prep         # no `agent:` map → membership-only: listed
        id: meeting_prep_agent     # on the team, no agent yaml generated
        required: false
    workflows: [{id: ..., steps: [...]}, ...]
                                   # full workflow docs written to workflows/ at
                                   # scaffold time — a recipe is a self-contained
                                   # installable unit
    files: / templates:            # extra workspace files, templated, createOnly

`kind: agent` recipes define one top-level `agent:` map instead of `agents:`;
the solo agent gets its own workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

import hashlib
import re

from jigga.core.io import ensure_dir, read_json, write_json, write_yaml
from jigga.core.models import TeamConfig, now_iso
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


def defines_agent(spec: dict[str, Any]) -> bool:
    """True when a team-recipe member spec defines an agent to scaffold (it
    carries an `agent:` map). A member with only id/role/required is
    membership-only: listed on the team, no agent yaml written — how a team
    references optional roles the user staffs later (e.g. meeting_prep_agent)."""
    return isinstance(spec.get("agent"), dict)


def _finalize_agent_doc(agent_id: str, definition: dict[str, Any],
                        ctx: dict[str, str]) -> dict[str, Any]:
    """Template the `agent:` definition (full agent-yaml passthrough — every
    AgentConfig field), apply scaffold defaults, and fold `cronJobs` sugar into
    `wake.schedules` (merged with any `wake:` the definition already carries —
    events, accepts_agent_requests, explicit schedules)."""
    doc: dict[str, Any] = {"id": agent_id}
    doc.update(_template({k: v for k, v in definition.items() if k != "cronJobs"}, ctx))
    doc.setdefault("memory_scope", "task_only")
    doc.setdefault("model", "profile:default")
    doc.setdefault("tools", [])
    doc.setdefault("permissions", {"network": {"mode": "ask"}, "shell": {"mode": "deny"}})
    wake = dict(doc.get("wake") or {})
    cron_wake = _wake_from_cronjobs(definition.get("cronJobs"), ctx)
    if cron_wake:
        wake["schedules"] = list(wake.get("schedules") or []) + cron_wake["schedules"]
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


def _write_recipe_workflows(workflows_dir: Path, recipe: Recipe, ctx: dict[str, str],
                            *, overwrite: bool) -> dict[str, list[str]]:
    """Write the recipe's embedded `workflows:` (full workflow documents in the
    frontmatter) into the runtime workflows dir — a recipe is a self-contained
    installable unit: agents + team + the workflows they run. Templated;
    create-only unless `overwrite` (same contract as agents/teams)."""
    written: list[str] = []
    skipped: list[str] = []
    for doc in recipe.meta.get("workflows") or []:
        if not isinstance(doc, dict) or not doc.get("id"):
            continue
        doc = _template(doc, ctx)
        path = workflows_dir / f"{doc['id']}.yaml"
        if path.exists() and not overwrite:
            skipped.append(str(doc["id"]))
            continue
        ensure_dir(workflows_dir)
        write_yaml(path, doc)
        written.append(str(doc["id"]))
    return {"written": written, "skipped": skipped}


# --- install records ---------------------------------------------------------
# Scaffolding writes a provenance record per install: which recipe (id/version/
# source), what it created, and each artifact's content hash AS WRITTEN. This
# is what `jigga recipes installed` reads, and the pristine-vs-locally-edited
# signal `jigga update` (#88) reconciles against.

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _records_dir(home: Path) -> Path:
    return Path(home) / "state" / "recipes"


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _rel_to_home(path: Path, home: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(home).resolve()))
    except ValueError:
        return str(path)


def _record_install(home: Path, recipe: Recipe, *, scaffold_id: str,
                    managed: list[Path], written: list[Path]) -> dict[str, Any]:
    """Write/merge the install record. `managed` is every artifact the recipe
    owns; hashes are refreshed only for files `written` THIS run, so a re-
    scaffold that skips a user-edited file keeps the as-installed hash (the
    edit stays detectable as drift)."""
    home = Path(home)
    records_dir = _records_dir(home)
    ensure_dir(records_dir)
    record_path = records_dir / f"{_SAFE_NAME.sub('_', scaffold_id)}.json"
    existing = read_json(record_path) if record_path.exists() else {}
    hashes = dict(existing.get("hashes") or {})
    for path in written:
        digest = _sha256(path)
        if digest:
            hashes[_rel_to_home(path, home)] = digest
    record = {
        "recipe_id": recipe.id,
        "kind": recipe.kind,
        "version": recipe.version,
        "source": recipe.source,
        "scaffold_id": scaffold_id,
        "installed_at": existing.get("installed_at") or now_iso(),
        "updated_at": now_iso(),
        "artifacts": sorted({_rel_to_home(p, home) for p in managed}),
        "hashes": hashes,
    }
    write_json(record_path, record)
    return record


def installed_recipes(home: Path) -> list[dict[str, Any]]:
    """All install records, each annotated with current drift: `modified`
    (content no longer matches the as-written hash) and `missing` (artifact
    deleted). The CLI and the UI read this; `jigga update` (#88) will act on it."""
    home = Path(home)
    records_dir = _records_dir(home)
    if not records_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(records_dir.glob("*.json")):
        record = read_json(path)
        if not isinstance(record, dict):
            continue
        modified: list[str] = []
        missing: list[str] = []
        for rel, digest in (record.get("hashes") or {}).items():
            artifact = home / rel
            if not artifact.exists():
                missing.append(rel)
            elif _sha256(artifact) != digest:
                modified.append(rel)
        record["modified"] = sorted(modified)
        record["missing"] = sorted(missing)
        records.append(record)
    return records


def recipe_summary(recipe: Recipe) -> dict[str, Any]:
    """What a recipe would scaffold — for `jigga recipes show` (and the UI)."""
    summary: dict[str, Any] = {
        "id": recipe.id, "name": recipe.name, "kind": recipe.kind,
        "version": recipe.version, "description": recipe.description,
        "purpose": recipe.purpose, "source": recipe.source,
        "workflows": [str(w["id"]) for w in recipe.meta.get("workflows") or []
                      if isinstance(w, dict) and w.get("id")],
        "files": [str(f["path"]) for f in recipe.meta.get("files") or []
                  if isinstance(f, dict) and f.get("path")],
    }
    if recipe.kind == "agent":
        summary["agents"] = [{"id": recipe.id, "role": "solo", "scaffolded": True}]
    else:
        summary["agents"] = [
            {"id": str(spec.get("id") or "{{teamId}}-" + str(spec.get("role") or "?")),
             "role": str(spec.get("role") or spec.get("id") or "?"),
             "required": bool(spec.get("required", True)),
             "scaffolded": defines_agent(spec)}
            for spec in recipe.agents
        ]
    return summary


def scaffold_agent(
    home: Path, recipe: Recipe, *, agent_id: str | None = None, overwrite: bool = False,
    agents_dir: Path | None = None, workflows_dir: Path | None = None,
) -> dict[str, Any]:
    """Scaffold a single agent from a `kind: agent` recipe (its top-level
    `agent:` map defines the agent), plus any embedded `workflows:`.
    Create-only unless `overwrite`."""
    if recipe.kind != "agent":
        raise ValueError(f"scaffold_agent requires kind: agent (got {recipe.kind!r})")
    agent_map = recipe.meta.get("agent")
    if not isinstance(agent_map, dict):
        raise ValueError(f"Recipe {recipe.id!r} (kind: agent) needs an `agent:` map defining the agent")
    home = Path(home)
    agent_id = agent_id or recipe.id
    agents_dir = agents_dir or home / "agents"
    workflows_dir = workflows_dir or home / "workflows"
    ensure_dir(agents_dir)
    ctx = {"agentId": agent_id, "agentName": recipe.name, "teamId": agent_id, "teamName": recipe.name}
    definition = dict(agent_map)
    definition.setdefault("name", recipe.name)
    definition.setdefault("role", recipe.description or recipe.name)
    doc = _finalize_agent_doc(agent_id, definition, ctx)
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
    workflows = _write_recipe_workflows(workflows_dir, recipe, ctx, overwrite=overwrite)
    managed = [path] + [workflows_dir / f"{wid}.yaml"
                        for wid in workflows["written"] + workflows["skipped"]]
    written_paths = ([path] if written else []) + [workflows_dir / f"{wid}.yaml"
                                                   for wid in workflows["written"]]
    _record_install(home, recipe, scaffold_id=agent_id, managed=managed, written=written_paths)
    return {"kind": "agent", "agent_id": agent_id, "agent_file": str(path), "written": written,
            "scheduled": bool(doc.get("wake")), "workspace": workspace["workspace"],
            "files_written": files["written"], "files_skipped": files["skipped"],
            "workflows_written": workflows["written"], "workflows_skipped": workflows["skipped"]}


def scaffold_team(
    home: Path, recipe: Recipe, *, team_id: str | None = None, overwrite: bool = False,
    agents_dir: Path | None = None, teams_dir: Path | None = None,
    workflows_dir: Path | None = None,
) -> dict[str, Any]:
    """Generate agent YAMLs + a team YAML + embedded workflows from a recipe,
    then scaffold the workspace. Members default to `<teamId>-<role>` ids; an
    explicit `id:` pins one. Members without an agent definition are
    membership-only (listed on the team, no yaml). Existing files are skipped
    unless `overwrite`."""
    if recipe.kind != "team":
        raise ValueError(f"scaffold_team requires kind: team (got {recipe.kind!r})")
    home = Path(home)
    team_id = team_id or recipe.id
    agents_dir = agents_dir or home / "agents"
    teams_dir = teams_dir or home / "teams"
    workflows_dir = workflows_dir or home / "workflows"
    ensure_dir(agents_dir)
    ensure_dir(teams_dir)
    ctx = {"teamId": team_id, "teamName": recipe.name}
    meta = recipe.meta

    lead_role = recipe.routing.get("lead") or (recipe.agents[0].get("role") if recipe.agents else None)
    members: list[dict[str, Any]] = []
    written: list[str] = []
    skipped: list[str] = []
    for spec in recipe.agents:
        role = str(spec.get("role") or spec.get("id"))
        agent_id = str(spec.get("id") or f"{team_id}-{role}")
        members.append({"id": agent_id, "role": role, "required": bool(spec.get("required", True))})
        if not defines_agent(spec):
            continue  # membership-only: on the roster, staffed later
        definition = dict(spec["agent"])
        definition.setdefault("name", role.title())
        definition.setdefault("role", role)
        agent_doc = _finalize_agent_doc(agent_id, definition, ctx)
        path = agents_dir / f"{agent_id}.yaml"
        if path.exists() and not overwrite:
            skipped.append(agent_id)
        else:
            write_yaml(path, agent_doc)
            written.append(agent_id)

    lead_id = None
    if lead_role:
        lead_id = next((m["id"] for m in members if m["role"] == str(lead_role)),
                       f"{team_id}-{lead_role}")
    elif members:
        lead_id = members[0]["id"]
    # `routing:` passes through whole (default_assignee, handoffs, ...);
    # `lead:` is recipe-only sugar resolved to default_assignee above.
    routing = _template({k: v for k, v in dict(meta.get("routing") or {}).items() if k != "lead"}, ctx)
    routing.setdefault("default_assignee", lead_id)
    team_doc: dict[str, Any] = {
        "id": team_id,
        "name": _template(recipe.name, ctx),
        "purpose": _template(recipe.purpose, ctx) if recipe.purpose else None,
        "agents": members,
        "routing": routing,
    }
    for key in ("memory_scope", "default_workflows", "policies"):
        if meta.get(key) is not None:
            team_doc[key] = _template(meta[key], ctx)
    team_path = teams_dir / f"{team_id}.yaml"
    team_written = overwrite or not team_path.exists()
    if team_written:
        write_yaml(team_path, team_doc)

    workspace = scaffold_workspace(home, TeamConfig.from_dict(team_doc))
    files = _write_recipe_files(Path(workspace["workspace"]), recipe, ctx, overwrite=overwrite)
    workflows = _write_recipe_workflows(workflows_dir, recipe, ctx, overwrite=overwrite)
    managed = ([agents_dir / f"{aid}.yaml" for aid in written + skipped] + [team_path]
               + [workflows_dir / f"{wid}.yaml" for wid in workflows["written"] + workflows["skipped"]])
    written_paths = ([agents_dir / f"{aid}.yaml" for aid in written]
                     + ([team_path] if team_written else [])
                     + [workflows_dir / f"{wid}.yaml" for wid in workflows["written"]])
    _record_install(home, recipe, scaffold_id=team_id, managed=managed, written=written_paths)
    return {
        "team_id": team_id, "team_file": str(team_path), "team_written": team_written,
        "agents_written": written, "agents_skipped": skipped, "lead": lead_id,
        "workspace": workspace["workspace"],
        "files_written": files["written"], "files_skipped": files["skipped"],
        "workflows_written": workflows["written"], "workflows_skipped": workflows["skipped"],
    }


# --- staffing: membership-only member → defined agent, recipe-first ------------


def emit_recipe(meta: dict[str, Any], body: str) -> str:
    """Recipe markdown from frontmatter + body. Programmatic writes re-emit the
    YAML, so comments in the (user-dir) copy are lost — the documented cost of
    recipe-as-source-of-truth staffing."""
    frontmatter = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=100)
    return f"---\n{frontmatter}---\n\n{body.strip()}\n"


def _minimal_agent_definition(member_id: str, role_text: str) -> dict[str, Any]:
    """The batteries-included minimum every agent gets (policy: memory.search
    for all agents; safe permission defaults)."""
    return {
        "name": member_id.replace("_", " ").replace("-", " ").title(),
        "role": role_text,
        "memory_scope": "task_only",
        "model": "profile:default",
        "tools": ["memory.search"],
        "permissions": {"network": {"mode": "ask"}, "shell": {"mode": "deny"}},
    }


def staff_member(paths: Any, team_id: str, member_id: str, *,
                 role_text: str | None = None) -> dict[str, Any]:
    """Staff a team member, recipe-first (the recipe stays the source of
    truth): write an `agent:` definition into the member's entry in the
    USER-dir recipe copy, repoint the install record at that copy, then
    create-only re-scaffold so the new agent yaml is generated BY the recipe
    (hashed into the install record — `jigga update` manages it forever).
    A member not on the roster is appended (optional) and staffed."""
    home = Path(paths.home)
    record_path = _records_dir(home) / f"{_SAFE_NAME.sub('_', team_id)}.json"
    if not record_path.exists():
        raise ValueError(f"Team {team_id!r} has no install record — it wasn't scaffolded "
                         "from a recipe. Define the agent yaml by hand instead.")
    record = read_json(record_path)
    source = Path(str(record.get("source") or ""))
    if not source.exists():
        raise ValueError(f"Recipe source {source} no longer exists")
    recipe = load_recipe(source)
    if recipe.kind != "team":
        raise ValueError(f"{team_id!r} was scaffolded from a non-team recipe")

    meta = dict(recipe.meta)
    members = [dict(m) for m in (meta.get("agents") or [])]
    target = None
    for member in members:
        explicit = str(member.get("id") or f"{team_id}-{member.get('role')}")
        if explicit == member_id:
            target = member
            break
    appended = target is None
    if target is None:
        target = {"id": member_id, "role": role_text or member_id, "required": False}
        members.append(target)
    if isinstance(target.get("agent"), dict):
        raise ValueError(f"{member_id!r} is already staffed (it has an agent definition)")
    role = role_text or str(target.get("role") or member_id)
    target["agent"] = _minimal_agent_definition(member_id, role)
    meta["agents"] = members

    user_copy = home / "recipes" / source.name
    user_copy.parent.mkdir(parents=True, exist_ok=True)
    user_copy.write_text(emit_recipe(meta, recipe.body), encoding="utf-8")
    load_recipe(user_copy)  # validate the emitted recipe before touching anything else

    # No explicit record repoint needed: the scaffold's _record_install stamps
    # the record with the recipe source it ran from — the user copy.
    summary = scaffold_team(home, load_recipe(user_copy), team_id=team_id,
                            agents_dir=home / "agents", teams_dir=home / "teams",
                            workflows_dir=home / "workflows")
    if appended:
        # The live team yaml already exists, so the create-only scaffold skipped
        # it — append the new roster entry there explicitly (additive only).
        team_path = home / "teams" / f"{team_id}.yaml"
        if team_path.exists():
            team_doc = yaml.safe_load(team_path.read_text(encoding="utf-8")) or {}
            roster = list(team_doc.get("agents") or [])
            if not any(isinstance(m, dict) and m.get("id") == member_id for m in roster):
                roster.append({"id": member_id, "role": str(target.get("role")), "required": False})
                team_doc["agents"] = roster
                write_yaml(team_path, team_doc)
    from jigga.runtime.audit import append_event

    append_event(paths.logs, "team.member_staffed", team=team_id, member=member_id,
                 recipe=str(user_copy))
    return {"team": team_id, "member": member_id, "recipe": str(user_copy),
            "agent_written": member_id in summary["agents_written"],
            "scaffold": summary}

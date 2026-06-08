"""Deleting agents and teams — the most destructive verbs in JIGGA, so:
everything removed is backed up first (state/backups/<date>/…), deletions are
recipe-aware (an owned member is DE-STAFFED in the recipe copy — back to
membership-only — so the recipe stays the source of truth), and every step is
audited. ClawKitchen parity: the agent editor's [Delete agent] and the team
editor's [Delete Team].
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from jigga.core.io import read_json, write_json
from jigga.core.models import now_iso
from jigga.runtime.audit import append_event
from jigga.runtime.recipes import (
    _records_dir,
    _SAFE_NAME,
    emit_recipe,
    installed_recipes,
    load_recipe,
)
from jigga.runtime.workspaces import workspace_dir


def _backup_root(home: Path) -> Path:
    return Path(home) / "state" / "backups" / now_iso()[:10]


def _backup_file(home: Path, path: Path, rel: str) -> str | None:
    if not path.exists():
        return None
    target = _backup_root(home) / rel
    counter = 1
    while target.exists():
        target = _backup_root(home) / f"{rel}.{counter}"
        counter += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    if path.is_dir():
        shutil.copytree(path, target)
    else:
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return str(target)


def _owning_record_for(home: Path, artifact_rel: str) -> dict[str, Any] | None:
    for record in installed_recipes(home):
        if artifact_rel in (record.get("artifacts") or []):
            return record
    return None


def _destaff_in_recipe(home: Path, record: dict[str, Any], agent_id: str) -> str | None:
    """Remove the member's `agent:` definition in the (user-dir) recipe copy —
    back to membership-only. Returns the copy's path, or None when the recipe
    is gone or isn't a team recipe."""
    source = Path(str(record.get("source") or ""))
    if not source.exists():
        return None
    recipe = load_recipe(source)
    if recipe.kind != "team":
        return None
    meta = dict(recipe.meta)
    scaffold_id = str(record.get("scaffold_id") or recipe.id)
    changed = False
    for member in meta.get("agents") or []:
        explicit = str(member.get("id") or f"{scaffold_id}-{member.get('role')}")
        if explicit == agent_id and isinstance(member.get("agent"), dict):
            del member["agent"]
            changed = True
    if not changed:
        return None
    user_copy = Path(home) / "recipes" / source.name
    user_copy.parent.mkdir(parents=True, exist_ok=True)
    user_copy.write_text(emit_recipe(meta, recipe.body), encoding="utf-8")
    return str(user_copy)


def _record_path_for(home: Path, scaffold_id: str) -> Path:
    return _records_dir(home) / f"{_SAFE_NAME.sub('_', scaffold_id)}.json"


def delete_agent(paths: Any, agent_id: str) -> dict[str, Any]:
    """Delete an agent: yaml + its workspace role dirs (backed up first). A
    recipe-owned member is de-staffed in the recipe (membership-only again) and
    dropped from the install record; a solo kind-agent install drops its
    record entirely. Team rosters keep the member entry (workflows/handoffs
    may reference it)."""
    home = Path(paths.home)
    agent_path = home / "agents" / f"{agent_id}.yaml"
    if not agent_path.exists():
        raise ValueError(f"No such agent: {agent_id!r}")

    backups: list[str] = []
    backup = _backup_file(home, agent_path, f"agents/{agent_id}.yaml")
    if backup:
        backups.append(backup)
    # Role dirs in every workspace that has one (its team's and/or its solo ws).
    workspaces_root = home / "workspaces"
    if workspaces_root.exists():
        for ws in sorted(p for p in workspaces_root.iterdir() if p.is_dir()):
            role_dir = ws / "roles" / agent_id
            if role_dir.exists():
                saved = _backup_file(home, role_dir, f"workspaces/{ws.name}/roles/{agent_id}")
                if saved:
                    backups.append(saved)
                shutil.rmtree(role_dir, ignore_errors=True)
    solo_ws = workspace_dir(home, agent_id)
    if solo_ws.exists():
        saved = _backup_file(home, solo_ws, f"workspaces/{agent_id}")
        if saved:
            backups.append(saved)
        shutil.rmtree(solo_ws, ignore_errors=True)

    record = _owning_record_for(home, f"agents/{agent_id}.yaml")
    destaffed_recipe = None
    if record is not None:
        if record.get("kind") == "agent" and record.get("scaffold_id") == agent_id:
            _record_path_for(home, agent_id).unlink(missing_ok=True)
        else:
            destaffed_recipe = _destaff_in_recipe(home, record, agent_id)
            record_path = _record_path_for(home, str(record.get("scaffold_id")))
            if record_path.exists():
                rec = read_json(record_path)
                if isinstance(rec, dict):
                    rec["artifacts"] = [a for a in (rec.get("artifacts") or [])
                                        if a != f"agents/{agent_id}.yaml"]
                    (rec.get("hashes") or {}).pop(f"agents/{agent_id}.yaml", None)
                    if destaffed_recipe:
                        rec["source"] = destaffed_recipe
                    rec["updated_at"] = now_iso()
                    write_json(record_path, rec)

    agent_path.unlink()
    append_event(paths.logs, "agent.deleted", agent=agent_id, backups=backups,
                 destaffed_recipe=destaffed_recipe)
    return {"agent": agent_id, "backups": backups, "destaffed_recipe": destaffed_recipe}


def delete_team(paths: Any, team_id: str) -> dict[str, Any]:
    """Delete a team: its yaml, its workspace, and the agents/workflows its
    install record OWNS (never shared or hand-written agents) — all backed up
    first. Hand-made teams (no record) lose only the yaml + workspace."""
    home = Path(paths.home)
    team_path = home / "teams" / f"{team_id}.yaml"
    if not team_path.exists():
        raise ValueError(f"No such team: {team_id!r}")

    backups: list[str] = []
    removed: list[str] = []
    record_path = _record_path_for(home, team_id)
    record = read_json(record_path) if record_path.exists() else None
    artifacts = list((record or {}).get("artifacts") or [])

    for rel in artifacts:
        target = home / rel
        if rel == f"teams/{team_id}.yaml" or not target.exists():
            continue
        saved = _backup_file(home, target, rel)
        if saved:
            backups.append(saved)
        target.unlink()
        removed.append(rel)

    saved = _backup_file(home, team_path, f"teams/{team_id}.yaml")
    if saved:
        backups.append(saved)
    team_path.unlink()
    removed.append(f"teams/{team_id}.yaml")

    ws = workspace_dir(home, team_id)
    if ws.exists():
        saved = _backup_file(home, ws, f"workspaces/{team_id}")
        if saved:
            backups.append(saved)
        shutil.rmtree(ws, ignore_errors=True)
        removed.append(f"workspaces/{team_id}")
    if record_path.exists():
        record_path.unlink()

    append_event(paths.logs, "team.deleted", team=team_id, removed=removed, backups=backups)
    return {"team": team_id, "removed": removed, "backups": backups}


def gc_workspaces(paths: Any, *, apply: bool = False,
                  protect: tuple[str, ...] = ()) -> dict[str, Any]:
    """Garbage-collect orphaned team/agent workspace dirs (no owning team or
    solo agent). Dry-run by default — returns the plan. With `apply=True`, each
    orphan is backed up to state/backups/<date>/ then removed, and the sweep is
    audited (`workspace.gc`)."""
    from jigga.runtime.workspaces import plan_workspace_gc

    home = Path(paths.home)
    orphans = plan_workspace_gc(home, paths.teams, paths.agents, protect=protect)
    if not apply:
        return {"applied": False, "orphans": orphans, "removed": [], "backups": []}

    removed: list[str] = []
    backups: list[str] = []
    for orphan in orphans:
        path = Path(orphan["path"])
        saved = _backup_file(home, path, f"workspaces/{orphan['id']}")
        if saved:
            backups.append(saved)
        shutil.rmtree(path, ignore_errors=True)
        removed.append(orphan["id"])
    append_event(paths.logs, "workspace.gc", removed=removed, backups=backups,
                 protect=list(protect))
    return {"applied": True, "orphans": orphans, "removed": removed, "backups": backups}

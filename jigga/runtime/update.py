"""`jigga update` — reconcile a runtime with the current code (#88).

Pulling a new version of the repo does NOT update what's already in
`~/.jigga/`: scaffolding is deliberately create-only (user edits are theirs),
config keys added by new features don't appear in old configs, and the
supervisor service keeps running the code it loaded at start. This module
computes a reconciliation PLAN and applies it on confirmation:

1. **Recipe artifacts** — for every install record (#91), regenerate the
   recipe's artifacts and three-way compare: file pristine (hash matches
   as-installed) but shipped content changed → update in place; file locally
   edited → leave alone, surface a notice; file missing → re-create.
2. **Config migrations** — additive key migrations for old configs (e.g.
   `channels.default` for installs that predate the default-channel key).
3. **Service refresh** — re-render the unit if its template changed and
   restart the supervisor either way, so the daemon runs the pulled code
   (preserving the installed `--interval-seconds`).

Plan first, mutate only in `apply_update` — every applied action is audited.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from jigga.core.io import read_json, read_yaml, write_json, write_yaml
from jigga.core.models import now_iso
from jigga.runtime.audit import append_event
from jigga.runtime.channels import ADAPTERS, ensure_default_channel
from jigga.runtime.recipes import (
    _records_dir,
    _sha256,
    installed_recipes,
    load_recipe,
    scaffold_agent,
    scaffold_team,
)
from jigga.runtime.service import install_service, status_service

_ARTIFACT_DIRS = ("agents", "teams", "workflows")


@dataclass
class UpdateAction:
    kind: str           # artifact.update | artifact.recreate | artifact.new | config.migrate | service.refresh
    description: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _regenerate_artifacts(record: dict[str, Any]) -> dict[str, str] | None:
    """Re-scaffold the record's recipe into a throwaway home and return
    {home-relative path: content} for every generated artifact. None when the
    recipe source no longer exists (surfaced as a notice by the planner)."""
    source = Path(str(record.get("source") or ""))
    if not source.exists():
        return None
    recipe = load_recipe(source)
    scaffold_id = str(record.get("scaffold_id") or recipe.id)
    with tempfile.TemporaryDirectory(prefix="jigga-update-") as tmp:
        tmp_home = Path(tmp)
        if recipe.kind == "agent":
            scaffold_agent(tmp_home, recipe, agent_id=scaffold_id,
                           agents_dir=tmp_home / "agents", workflows_dir=tmp_home / "workflows")
        else:
            scaffold_team(tmp_home, recipe, team_id=scaffold_id,
                          agents_dir=tmp_home / "agents", teams_dir=tmp_home / "teams",
                          workflows_dir=tmp_home / "workflows")
        generated: dict[str, str] = {}
        for sub in _ARTIFACT_DIRS:
            directory = tmp_home / sub
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.yaml")):
                generated[f"{sub}/{path.name}"] = path.read_text(encoding="utf-8")
        return generated


def _plan_recipe_actions(home: Path) -> tuple[list[UpdateAction], list[str], list[dict[str, Any]]]:
    actions: list[UpdateAction] = []
    notices: list[str] = []
    edited: list[dict[str, Any]] = []
    for record in installed_recipes(home):
        generated = _regenerate_artifacts(record)
        scaffold_id = record.get("scaffold_id") or record.get("recipe_id")
        if generated is None:
            notices.append(f"{scaffold_id}: recipe source {record.get('source')!r} no longer exists — skipped")
            continue
        hashes = record.get("hashes") or {}
        for rel, content in generated.items():
            current = Path(home) / rel
            detail = {"path": rel, "content": content, "record": str(record.get("scaffold_id"))}
            if not current.exists():
                kind = "artifact.recreate" if rel in (record.get("artifacts") or []) else "artifact.new"
                verb = "re-create missing" if kind == "artifact.recreate" else "create new"
                actions.append(UpdateAction(kind, f"{verb} {rel} (from recipe {record.get('recipe_id')})", detail))
                continue
            current_hash = _sha256(current)
            generated_hash = _sha256_text(content)
            if current_hash == generated_hash:
                continue  # already up to date
            if hashes.get(rel) and current_hash == hashes.get(rel):
                actions.append(UpdateAction(
                    "artifact.update",
                    f"update {rel} (pristine since install; shipped recipe changed)", detail))
            else:
                # Local edits + shipped changes: never auto-replaced. Returned
                # structured so the CLI can offer a per-item picker (backed up
                # before any replace); also a human notice for plan output.
                edited.append({"path": rel, "content": content,
                               "record": str(record.get("scaffold_id")),
                               "recipe": Path(str(record.get("source") or "")).stem})
                notices.append(f"{rel}: shipped recipe changed but your copy has local edits — kept")
    return actions, notices, edited


def _sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- config migrations (additive only; each returns a description or None) ----


def _migrate_channels_default(config: dict[str, Any]) -> str | None:
    channels = config.get("channels") or {}
    if not isinstance(channels, dict) or "default" in channels:
        return None
    enabled = [name for name, cfg in channels.items()
               if isinstance(cfg, dict) and cfg.get("enabled") and name in ADAPTERS]
    if not enabled:
        return None
    return f"set channels.default = {enabled[0]!r} (first enabled channel; predates the default-channel key)"


_CONFIG_MIGRATIONS: list[tuple[str, Any]] = [
    ("channels.default", _migrate_channels_default),
]


def _apply_config_migrations(config: dict[str, Any]) -> list[str]:
    applied: list[str] = []
    for key, plan_fn in _CONFIG_MIGRATIONS:
        description = plan_fn(config)
        if description is None:
            continue
        if key == "channels.default":
            channels = config.get("channels") or {}
            enabled = [name for name, cfg in channels.items()
                       if isinstance(cfg, dict) and cfg.get("enabled") and name in ADAPTERS]
            ensure_default_channel(config, enabled[0])
        applied.append(description)
    return applied


def _installed_interval(unit_path: Path) -> float:
    """The --interval-seconds the installed unit runs with (re-render must not
    silently reset a custom interval). Falls back to the 60s default."""
    try:
        text = unit_path.read_text(encoding="utf-8")
    except OSError:
        return 60.0
    match = re.search(r"--interval-seconds\D*([\d.]+)", text)
    return float(match.group(1)) if match else 60.0


def plan_update(paths: Any) -> dict[str, Any]:
    """Compute the reconciliation plan. Read-only — nothing is mutated."""
    home = Path(paths.home)
    actions, notices, edited = _plan_recipe_actions(home)

    config = read_yaml(paths.config) if paths.config.exists() else {}
    for _key, plan_fn in _CONFIG_MIGRATIONS:
        description = plan_fn(config)
        if description:
            actions.append(UpdateAction("config.migrate", description, {}))

    status = status_service(paths)
    if status.get("installed"):
        interval = _installed_interval(Path(str(status.get("unit_path") or "")))
        actions.append(UpdateAction(
            "service.refresh",
            f"re-render the {status.get('backend')} unit and restart the supervisor "
            f"(interval {int(interval)}s) so the daemon runs the current code",
            {"interval_seconds": interval}))

    return {"actions": [a.to_dict() for a in actions], "notices": notices, "edited": edited}


def apply_update(paths: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Apply a plan from `plan_update`. Every action is audited; artifact
    updates also refresh the install record's as-written hash so the file
    reads as pristine again."""
    home = Path(paths.home)
    applied: list[str] = []
    errors: list[str] = []
    config_dirty = False
    config = read_yaml(paths.config) if paths.config.exists() else {}

    for action in plan.get("actions", []):
        kind = action.get("kind")
        try:
            if kind in ("artifact.update", "artifact.recreate", "artifact.new"):
                rel = action["detail"]["path"]
                target = home / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(action["detail"]["content"], encoding="utf-8")
                _refresh_record_hash(home, action["detail"].get("record"), rel,
                                     _sha256_text(action["detail"]["content"]))
            elif kind == "config.migrate":
                applied_migrations = _apply_config_migrations(config)
                config_dirty = config_dirty or bool(applied_migrations)
            elif kind == "service.refresh":
                result = install_service(
                    paths, interval_seconds=float(action["detail"].get("interval_seconds") or 60.0))
                if result.get("backend") == "unsupported":
                    errors.append("service.refresh: no user service manager on this platform")
                    continue
            applied.append(action.get("description", kind))
            append_event(paths.logs, "update.applied", kind=kind,
                         description=action.get("description"))
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"{kind}: {exc}")
    if config_dirty:
        write_yaml(paths.config, config)
    return {"applied": applied, "errors": errors}


def backup_path_for(home: Path, rel: str, *, stamp: str) -> Path:
    return Path(home) / "state" / "backups" / stamp / rel


def overwrite_edited(paths: Any, plan: dict[str, Any], selected_paths: list[str]) -> dict[str, Any]:
    """Replace the SELECTED edited-divergent files with their regenerated
    content. Each file is backed up first (state/backups/<date>/<rel>) so the
    destructive choice stays reversible; the install record's hash refreshes so
    the file reads pristine again. Unselected files are never touched."""
    home = Path(paths.home)
    chosen = set(selected_paths)
    replaced: list[str] = []
    backups: list[str] = []
    errors: list[str] = []
    stamp = now_iso()[:10]
    for item in plan.get("edited", []):
        rel = item.get("path")
        if rel not in chosen:
            continue
        try:
            target = home / rel
            backup = backup_path_for(home, rel, stamp=stamp)
            counter = 1
            while backup.exists():  # same file replaced twice in one day
                backup = backup_path_for(home, f"{rel}.{counter}", stamp=stamp)
                counter += 1
            backup.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
                backups.append(str(backup))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item["content"], encoding="utf-8")
            _refresh_record_hash(home, item.get("record"), rel, _sha256_text(item["content"]))
            replaced.append(rel)
            append_event(paths.logs, "update.overwrote_edited", path=rel,
                         backup=str(backup), record=item.get("record"))
        except (OSError, KeyError, ValueError) as exc:
            errors.append(f"{rel}: {exc}")
    return {"replaced": replaced, "backups": backups, "errors": errors}


def _refresh_record_hash(home: Path, scaffold_id: str | None, rel: str, digest: str) -> None:
    if not scaffold_id:
        return
    record_path = _records_dir(home) / f"{scaffold_id}.json"
    if not record_path.exists():
        return
    record = read_json(record_path)
    if not isinstance(record, dict):
        return
    record.setdefault("hashes", {})[rel] = digest
    artifacts = set(record.get("artifacts") or [])
    artifacts.add(rel)
    record["artifacts"] = sorted(artifacts)
    record["updated_at"] = now_iso()
    write_json(record_path, record)

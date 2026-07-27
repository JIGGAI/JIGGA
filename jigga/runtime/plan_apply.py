from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from jigga.core.config import load_agents, load_memory_scopes, load_teams, load_workflows
from jigga.core.io import list_config_files, read_json, write_json
from jigga.core.models import now_iso
from jigga.core.paths import JiggaPaths
from jigga.runtime.audit import new_id

CONFIG_KINDS = ("agents", "teams", "workflows", "memory")
STATE_FILE = "applied-config.json"


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inventory_dir(path: Path, base: Path) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for file in list_config_files(path):
        key = str(file.relative_to(base))
        items[key] = {"sha256": _file_digest(file), "size": file.stat().st_size}
    return items


def current_inventory(paths: JiggaPaths) -> dict[str, Any]:
    return {
        "agents": _inventory_dir(paths.agents, paths.home),
        "teams": _inventory_dir(paths.teams, paths.home),
        "workflows": _inventory_dir(paths.workflows, paths.home),
        "memory": _inventory_dir(paths.memory, paths.home),
    }


def _load_applied(paths: JiggaPaths) -> dict[str, Any]:
    state_path = paths.state.parent / STATE_FILE
    if not state_path.exists():
        return {kind: {} for kind in CONFIG_KINDS}
    return read_json(state_path)


def _diff_kind(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key in sorted(after.keys() - before.keys()):
        changes.append({"path": key, "change": "create"})
    for key in sorted(before.keys() - after.keys()):
        changes.append({"path": key, "change": "delete"})
    for key in sorted(before.keys() & after.keys()):
        if before[key].get("sha256") != after[key].get("sha256"):
            changes.append({"path": key, "change": "update"})
    return changes


def _requires_approval(paths: JiggaPaths, path_key: str, change: str) -> str | None:
    if change == "delete":
        return "config.delete"
    if path_key.startswith("workflows/"):
        workflows = load_workflows(paths.workflows)
        for workflow in workflows.values():
            if workflow.source and str(Path(workflow.source).relative_to(paths.home)) == path_key:
                if workflow.trigger or any(step.approval == "required" for step in workflow.steps):
                    return "workflow.activation"
    if path_key.startswith("agents/"):
        agents = load_agents(paths.agents)
        for agent in agents.values():
            if agent.source and str(Path(agent.source).relative_to(paths.home)) == path_key:
                permissions = agent.permissions or {}
                if permissions.get("shell", {}).get("mode") in {"allow", "ask", "restricted"}:
                    return "permission.shell"
                if permissions.get("network", {}).get("mode") in {"allow", "ask"}:
                    return "permission.network"
    return None


def plan_runtime(paths: JiggaPaths) -> dict[str, Any]:
    before = _load_applied(paths)
    after = current_inventory(paths)
    changes: list[dict[str, Any]] = []
    approvals: list[dict[str, str]] = []
    for kind in CONFIG_KINDS:
        for change in _diff_kind(before.get(kind, {}), after.get(kind, {})):
            change["kind"] = kind
            reason = _requires_approval(paths, change["path"], change["change"])
            if reason:
                change["requires_approval"] = reason
                approvals.append({"path": change["path"], "reason": reason})
            changes.append(change)
    return {
        "id": new_id("plan"),
        "created_at": now_iso(),
        "status": "needs_approval" if approvals else "ready",
        "changes": changes,
        "approvals": approvals,
        "summary": {kind: len(_diff_kind(before.get(kind, {}), after.get(kind, {}))) for kind in CONFIG_KINDS},
    }


def apply_runtime(paths: JiggaPaths, approve: bool = False) -> dict[str, Any]:
    plan = plan_runtime(paths)
    if plan["approvals"] and not approve:
        return {"status": "needs_approval", "plan": plan}
    snapshot = current_inventory(paths)
    write_json(paths.state.parent / STATE_FILE, snapshot)
    return {"status": "applied", "applied_at": now_iso(), "plan": plan}


def validate_runtime_configs(paths: JiggaPaths) -> dict[str, Any]:
    from jigga.runtime.validation import validate_configs

    agents = load_agents(paths.agents)
    teams = load_teams(paths.teams)
    workflows = load_workflows(paths.workflows)
    return {
        "agents": sorted(agents.keys()),
        "teams": sorted(teams.keys()),
        "workflows": sorted(workflows.keys()),
        "memory_scopes": sorted(load_memory_scopes(paths.memory).keys()),
        # Fail-fast config checks (cron strings, routing.handoffs shape/members,
        # v2 workflow graphs).
        "problems": validate_configs(agents, teams, workflows),
    }

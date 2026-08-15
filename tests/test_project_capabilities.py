from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.paths import (
    project_capabilities_dir,
    resolve_project_root,
)
from jigga.runtime.capabilities import (
    CapabilityRegistry,
    load_capability_manifest,
    record_approval,
)
from jigga.runtime.workflow import run_workflow


# --- resolve_project_root (explicit-only) -----------------------------------


def test_resolve_project_root_is_explicit_only(tmp_path: Path, monkeypatch) -> None:
    """No --project, no env → no project, even when cwd sits inside a
    directory tree that contains a `.jigga/`. The removed cwd-walk used to
    cross out of the repo and declare the user's HOME a project (real
    ~/.jigga state bled into isolated tests); opting in is saying --project."""
    (tmp_path / ".jigga").mkdir()
    nested = tmp_path / "src" / "app"
    nested.mkdir(parents=True)
    monkeypatch.delenv("JIGGA_PROJECT", raising=False)
    monkeypatch.chdir(nested)
    assert resolve_project_root(None) is None


def test_resolve_project_root_explicit_wins_over_env(tmp_path: Path, monkeypatch) -> None:
    explicit_root = tmp_path / "explicit"
    env_root = tmp_path / "from_env"
    explicit_root.mkdir()
    env_root.mkdir()
    monkeypatch.setenv("JIGGA_PROJECT", str(env_root))
    assert resolve_project_root(explicit_root) == explicit_root.resolve()


def test_resolve_project_root_env_var_used_when_no_explicit(tmp_path: Path, monkeypatch) -> None:
    env_root = tmp_path / "from_env"
    env_root.mkdir()
    monkeypatch.setenv("JIGGA_PROJECT", str(env_root))
    assert resolve_project_root(None) == env_root.resolve()


def test_resolve_project_root_returns_none_for_missing_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("JIGGA_PROJECT", raising=False)
    monkeypatch.setenv("JIGGA_PROJECT", str(tmp_path / "does_not_exist"))
    # Env var points at non-existent path → return None rather than constructing
    # something the registry would try to scan.
    assert resolve_project_root(None) is None


def test_project_capabilities_dir_returns_canonical_subpath(tmp_path: Path) -> None:
    (tmp_path / ".jigga").mkdir()
    assert project_capabilities_dir(tmp_path) == (tmp_path / ".jigga" / "capabilities").resolve()


def test_project_capabilities_dir_returns_none_for_no_project() -> None:
    assert project_capabilities_dir(None) is None


# --- precedence: project > user > bundled ----------------------------------


def _write_pack(directory: Path, name: str, action: str, **extra) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "version": "1.0.0",
        "summary": f"{name} test pack",
        "actions": [action],
        "risk_level": "low",
    }
    data.update(extra)
    write_yaml(directory / "manifest.yaml", data)


def test_project_capability_overrides_user_for_same_action(tmp_path: Path) -> None:
    user_dir = tmp_path / "user" / "writer"
    project_dir = tmp_path / "project" / ".jigga" / "capabilities" / "writer"
    _write_pack(user_dir, "user-writer", "writer.run")
    _write_pack(project_dir, "project-writer", "writer.run")

    registry = CapabilityRegistry.load(
        user_capabilities=tmp_path / "user",
        project_capabilities=tmp_path / "project" / ".jigga" / "capabilities",
    )
    assert registry.resolve_action("writer.run").name == "project-writer"


def test_project_capability_overrides_bundled_for_same_action(tmp_path: Path) -> None:
    # Override the bundled `calendar` capability's action.
    project_dir = tmp_path / "project" / ".jigga" / "capabilities" / "custom-calendar"
    _write_pack(project_dir, "custom-calendar", "calendar.list_events")
    registry = CapabilityRegistry.load(
        project_capabilities=tmp_path / "project" / ".jigga" / "capabilities",
    )
    assert registry.resolve_action("calendar.list_events").name == "custom-calendar"


def test_user_capability_used_when_no_project_pack_for_action(tmp_path: Path) -> None:
    user_dir = tmp_path / "user" / "writer"
    _write_pack(user_dir, "user-writer", "writer.run")
    registry = CapabilityRegistry.load(
        user_capabilities=tmp_path / "user",
        project_capabilities=tmp_path / "project" / ".jigga" / "capabilities",  # empty / doesn't exist
    )
    assert registry.resolve_action("writer.run").name == "user-writer"


# --- approvals apply to project packs too ----------------------------------


def test_project_capability_requires_first_use_approval(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    project_root = tmp_path / "project"
    project_cap_dir = project_root / ".jigga" / "capabilities" / "demo"
    _write_pack(project_cap_dir, "project-demo", "project.demo")

    # Unapproved: goes to pending.
    gated = CapabilityRegistry.load(
        project_capabilities=project_root / ".jigga" / "capabilities",
        approvals_dir=paths.policies,
    )
    assert gated.get("project-demo") is None
    assert any(c.name == "project-demo" for c in gated.list_pending())

    # Approve and reload: now active.
    record_approval(paths.policies, load_capability_manifest(project_cap_dir / "manifest.yaml"))
    approved_registry = CapabilityRegistry.load(
        project_capabilities=project_root / ".jigga" / "capabilities",
        approvals_dir=paths.policies,
    )
    assert approved_registry.get("project-demo") is not None


# --- CLI integration -------------------------------------------------------


def test_cli_capabilities_list_picks_up_project_pack(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path, examples=True)
    project_root = tmp_path / "project"
    project_cap_dir = project_root / ".jigga" / "capabilities" / "mycap"
    _write_pack(project_cap_dir, "mycap", "mycap.run")
    # Approve so it lands in active.
    record_approval(paths.policies, load_capability_manifest(project_cap_dir / "manifest.yaml"))

    assert main([
        "--home",
        str(tmp_path),
        "--project",
        str(project_root),
        "capabilities",
        "list",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    actions = payload["actions"]
    assert actions.get("mycap.run") == "mycap"


def test_cli_workflow_run_uses_project_capability(tmp_path: Path, grant) -> None:
    paths = init_runtime(tmp_path, examples=True)
    grant(paths, "daily_briefing_agent", "proj.skill_run")
    project_root = tmp_path / "project"
    project_cap_dir = project_root / ".jigga" / "capabilities" / "proj-skill"
    project_cap_dir.mkdir(parents=True)
    (project_cap_dir / "instructions.md").write_text("Echo back the input.", encoding="utf-8")
    write_yaml(
        project_cap_dir / "manifest.yaml",
        {
            "name": "proj-skill",
            "version": "1.0.0",
            "summary": "Project skill demo",
            "type": "skill_pack",
            "actions": ["proj.skill_run"],
            "instructions": "instructions.md",
            "risk_level": "low",
        },
    )
    record_approval(paths.policies, load_capability_manifest(project_cap_dir / "manifest.yaml"))
    write_yaml(
        paths.workflows / "proj_wf.yaml",
        {
            "id": "proj_wf",
            "name": "Project workflow",
            "steps": [
                {
                    "id": "run",
                    "agent": "daily_briefing_agent",
                    "action": "proj.skill_run",
                    "input": {"topic": "test"},
                }
            ],
        },
    )
    project_cap_path = project_root / ".jigga" / "capabilities"
    result = run_workflow(
        paths,
        "proj_wf",
        project_capabilities=project_cap_path,
    )
    assert result["status"] == "completed"
    assert result["outputs"]["run"]["source"] == "capability.skill_pack"
    assert result["outputs"]["run"]["skill"] == "proj-skill"


def test_cli_project_flag_default_is_none_so_existing_runs_unchanged(tmp_path: Path) -> None:
    # Regression check: when --project is omitted and there's no .jigga above
    # the tmp_path (which the fixture establishes), the registry behavior is
    # identical to before this PR.
    paths = init_runtime(tmp_path, examples=True)
    # Run a bundled workflow without specifying --project; should still work.
    result = run_workflow(
        paths, "morning_day_summary"
    )
    assert result["status"] == "completed"

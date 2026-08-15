from __future__ import annotations

from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_workflows
from jigga.core.io import write_yaml
from jigga.runtime.capabilities import CapabilityRegistry, load_capability_manifest, record_approval
from jigga.runtime.workflow import plan_workflow, run_workflow


def test_registry_loads_bundled_capabilities() -> None:
    registry = CapabilityRegistry.load()
    calendar = registry.get("calendar")
    assert calendar is not None
    assert registry.resolve_action("calendar.list_events") == calendar
    assert registry.resolve_action("notifications.send").name == "notifications"


def test_user_capability_overrides_bundled_action(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capabilities" / "custom-calendar"
    cap_dir.mkdir(parents=True)
    write_yaml(
        cap_dir / "manifest.yaml",
        {
            "name": "custom-calendar",
            "version": "1.0.0",
            "summary": "Custom calendar adapter.",
            "actions": ["calendar.list_events"],
            "risk_level": "medium",
        },
    )
    registry = CapabilityRegistry.load(user_capabilities=tmp_path / "capabilities")
    assert registry.resolve_action("calendar.list_events").name == "custom-calendar"


def test_capability_manifest_validation_requires_actions(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    write_yaml(manifest, {"name": "broken", "version": "0.1.0", "summary": "No actions"})
    try:
        load_capability_manifest(manifest)
    except ValueError as exc:
        assert "actions" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected validation failure")


def test_workflow_plan_surfaces_capability_metadata(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    workflow = load_workflows(paths.workflows)["morning_day_summary"]
    plan = plan_workflow(
        workflow,
        load_agents(paths.agents),
        registry=CapabilityRegistry.load(user_capabilities=paths.capabilities),
    )
    assert plan["can_run"] is True
    first = plan["steps"][0]["policy"]
    assert first["capability"] == "calendar"
    assert first["risk_level"] == "low"


def test_memory_context_does_not_leak_runtime_plumbing(tmp_path: Path) -> None:
    # The dispatcher used to pass a single dict carrying both memory context
    # and runtime plumbing (home, logs_dir, sessions_dir, agent), so the
    # summarization handler had to manually filter. With memory_context and
    # runtime split, no runtime key should appear in the memory_context that
    # handlers receive.
    paths = init_runtime(tmp_path, examples=True)
    result = run_workflow(paths, "morning_day_summary")
    assert result["status"] == "completed"
    summary_step = result["outputs"]["summarize"]
    leaked_keys = {"home", "logs_dir", "sessions_dir", "agent"}
    assert leaked_keys.isdisjoint(summary_step["memory_context"].keys())


def test_workflow_run_dispatches_through_capabilities(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    result = run_workflow(paths, "morning_day_summary")
    assert result["status"] == "completed"
    assert result["outputs"]["read_calendar"][0]["source"] == "capability.dry_run"
    logs = "\n".join(item.read_text(encoding="utf-8") for item in paths.logs.glob("*.jsonl"))
    assert "capability.invocation.started" in logs
    assert "capability.invocation.completed" in logs


def test_unknown_workflow_action_blocks_cleanly(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(
        paths.workflows / "unknown.yaml",
        {
            "id": "unknown",
            "name": "Unknown",
            "steps": [{"id": "do_it", "agent": "daily_briefing_agent", "action": "missing.action"}],
        },
    )
    result = run_workflow(paths, "unknown")
    assert result["status"] == "blocked"
    assert result["plan"]["steps"][0]["policy"]["permission"] == "capability.available"


def test_capabilities_cli_smoke(tmp_path: Path, capsys) -> None:
    assert main(["--home", str(tmp_path), "init", "--examples"]) == 0
    assert main(["--home", str(tmp_path), "capabilities", "list"]) == 0
    assert "calendar.list_events" in capsys.readouterr().out
    assert main(["--home", str(tmp_path), "capabilities", "inspect", "calendar"]) == 0
    assert '"name": "calendar"' in capsys.readouterr().out


def test_user_capability_is_pending_until_first_use_approval(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    cap_dir = paths.capabilities / "needs-approval"
    cap_dir.mkdir(parents=True)
    write_yaml(
        cap_dir / "manifest.yaml",
        {
            "name": "needs-approval",
            "version": "1.0.0",
            "summary": "User pack that should be gated.",
            "actions": ["custom.demo"],
            "risk_level": "low",
        },
    )
    gated = CapabilityRegistry.load(
        user_capabilities=paths.capabilities, approvals_dir=paths.policies
    )
    assert gated.get("needs-approval") is None  # not active
    assert any(cap.name == "needs-approval" for cap in gated.list_pending())

    record_approval(paths.policies, load_capability_manifest(cap_dir / "manifest.yaml"))
    after = CapabilityRegistry.load(
        user_capabilities=paths.capabilities, approvals_dir=paths.policies
    )
    assert after.get("needs-approval") is not None
    assert after.list_pending() == []


def test_approval_invalidated_by_manifest_hash_change(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    cap_dir = paths.capabilities / "drift"
    cap_dir.mkdir(parents=True)
    manifest_path = cap_dir / "manifest.yaml"
    write_yaml(
        manifest_path,
        {"name": "drift", "version": "1.0.0", "summary": "v1", "actions": ["drift.run"]},
    )
    record_approval(paths.policies, load_capability_manifest(manifest_path))
    # Manifest changes (e.g. version bump) → hash mismatch → falls back to pending.
    write_yaml(
        manifest_path,
        {"name": "drift", "version": "2.0.0", "summary": "v2", "actions": ["drift.run"]},
    )
    registry = CapabilityRegistry.load(
        user_capabilities=paths.capabilities, approvals_dir=paths.policies
    )
    assert registry.get("drift") is None
    assert any(cap.name == "drift" for cap in registry.list_pending())


def test_capabilities_approve_cli_records_approval(tmp_path: Path, capsys) -> None:
    import json

    paths = init_runtime(tmp_path, examples=True)
    cap_dir = paths.capabilities / "cli-demo"
    cap_dir.mkdir(parents=True)
    manifest_path = cap_dir / "manifest.yaml"
    write_yaml(
        manifest_path,
        {"name": "cli-demo", "version": "1.0.0", "summary": "x", "actions": ["cli.x"]},
    )
    assert main(["--home", str(tmp_path), "capabilities", "approve", str(manifest_path)]) == 0
    pending_output = json.loads(capsys.readouterr().out)
    assert pending_output["status"] == "needs_approval"

    assert (
        main(
            ["--home", str(tmp_path), "capabilities", "approve", str(manifest_path), "--approve"]
        )
        == 0
    )
    approved_output = json.loads(capsys.readouterr().out)
    assert approved_output["status"] == "approved"
    assert approved_output["capability"] == "cli-demo"

    assert main(["--home", str(tmp_path), "capabilities", "pending"]) == 0
    assert capsys.readouterr().out.strip() == "[]"


def test_dispatcher_resolves_handler_via_dotted_import_path(tmp_path: Path, grant) -> None:
    # A user-local capability declares its handler as `module.path:function`.
    # The dispatcher imports it lazily and calls it. This is the extensibility
    # path: third-party packs no longer need to monkey-patch HANDLERS.
    paths = init_runtime(tmp_path, examples=True)
    cap_dir = paths.capabilities / "custom-pack"
    cap_dir.mkdir(parents=True)
    write_yaml(
        cap_dir / "manifest.yaml",
        {
            "name": "custom-pack",
            "version": "1.0.0",
            "summary": "Imports its handler dynamically.",
            "actions": ["custom.run"],
            "handler": "tests.fixtures.capability_handlers:custom_handler",
            "risk_level": "low",
        },
    )
    write_yaml(
        paths.workflows / "custom.yaml",
        {
            "id": "custom",
            "name": "Custom",
            "steps": [{"id": "run_custom", "agent": "daily_briefing_agent", "action": "custom.run"}],
        },
    )
    # User-local capability needs first-use approval before run_workflow's
    # registry will dispatch through it, and the agent needs the grant.
    grant(paths, "daily_briefing_agent", "custom.run")
    record_approval(paths.policies, load_capability_manifest(cap_dir / "manifest.yaml"))
    result = run_workflow(
        paths, "custom"
    )
    assert result["status"] == "completed"
    assert result["outputs"]["run_custom"]["marker"] == "custom_handler_was_called"


def test_dispatcher_rejects_invalid_handler_paths() -> None:
    import pytest

    from jigga.runtime.dispatcher import resolve_handler

    with pytest.raises(ValueError, match="Cannot import"):
        resolve_handler("nonexistent_module_xyz:run")
    with pytest.raises(ValueError, match="must be either"):
        resolve_handler("only_module_no_colon")
    with pytest.raises(ValueError, match="resolved to non-callable"):
        resolve_handler("tests.fixtures.capability_handlers:__doc__")


def test_user_capability_manifest_hash_is_recorded(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capabilities" / "custom-calendar"
    cap_dir.mkdir(parents=True)
    manifest = cap_dir / "manifest.yaml"
    write_yaml(
        manifest,
        {
            "name": "custom-calendar",
            "version": "1.0.0",
            "summary": "Custom calendar adapter.",
            "actions": ["calendar.list_events"],
        },
    )
    capability = load_capability_manifest(manifest)
    assert capability.manifest_hash is not None
    assert len(capability.manifest_hash) == 64


def test_symlinked_capability_manifest_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.yaml"
    write_yaml(target, {"name": "x", "version": "1", "summary": "x", "actions": ["x.y"]})
    link_dir = tmp_path / "capabilities" / "linked"
    link_dir.mkdir(parents=True)
    link = link_dir / "manifest.yaml"
    link.symlink_to(target)
    try:
        load_capability_manifest(link)
    except ValueError as exc:
        assert "symlink" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected symlink rejection")


def test_duplicate_user_action_resolution_is_first_wins(tmp_path: Path) -> None:
    for name in ("aaa-first", "zzz-second"):
        cap_dir = tmp_path / "capabilities" / name
        cap_dir.mkdir(parents=True)
        write_yaml(
            cap_dir / "manifest.yaml",
            {
                "name": name,
                "version": "1.0.0",
                "summary": name,
                "actions": ["demo.action"],
            },
        )
    registry = CapabilityRegistry.load(user_capabilities=tmp_path / "capabilities")
    assert registry.resolve_action("demo.action").name == "aaa-first"


def test_medium_risk_capability_requires_approval_under_ask_mode(tmp_path: Path) -> None:
    # A user-local capability marked `medium` overrides the bundled `calendar`
    # action so we can exercise the risk-level approval gate without depending
    # on whatever the bundled capabilities' risk levels happen to be.
    cap_dir = tmp_path / "capabilities" / "medium-calendar"
    cap_dir.mkdir(parents=True)
    write_yaml(
        cap_dir / "manifest.yaml",
        {
            "name": "medium-calendar",
            "version": "1.0.0",
            "summary": "Medium-risk shadow of the calendar capability.",
            "actions": ["calendar.list_events"],
            "risk_level": "medium",
        },
    )
    paths = init_runtime(tmp_path, examples=True)
    workflow = load_workflows(paths.workflows)["morning_day_summary"]
    plan = plan_workflow(
        workflow,
        load_agents(paths.agents),
        default_mode="ask",
        registry=CapabilityRegistry.load(user_capabilities=tmp_path / "capabilities"),
    )
    first = plan["steps"][0]["policy"]
    assert first["capability"] == "medium-calendar"
    assert first["status"] == "needs_approval"
    assert first["permission"] == "capability.risk_level"


def test_medium_risk_capability_can_run_under_autonomous_mode(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capabilities" / "medium-demo"
    cap_dir.mkdir(parents=True)
    write_yaml(
        cap_dir / "manifest.yaml",
        {
            "name": "medium-demo",
            "version": "1.0.0",
            "summary": "Medium risk with no resource permissions.",
            "actions": ["calendar.list_events"],
            "risk_level": "medium",
        },
    )
    paths = init_runtime(tmp_path, examples=True)
    workflow = load_workflows(paths.workflows)["morning_day_summary"]
    plan = plan_workflow(
        workflow,
        load_agents(paths.agents),
        default_mode="autonomous",
        registry=CapabilityRegistry.load(user_capabilities=tmp_path / "capabilities"),
    )
    first = plan["steps"][0]["policy"]
    assert first["capability"] == "medium-demo"
    assert first["status"] == "allow"


def test_capability_calendar_permission_requires_agent_grant(tmp_path: Path, grant) -> None:
    cap_dir = tmp_path / "capabilities" / "writer-cap"
    cap_dir.mkdir(parents=True)
    write_yaml(
        cap_dir / "manifest.yaml",
        {
            "name": "writer-cap",
            "version": "1.0.0",
            "summary": "Needs calendar but agent only grants email.",
            "actions": ["calendar.list_events"],
            "permissions": {"calendar": "read"},
            "risk_level": "low",
        },
    )
    paths = init_runtime(tmp_path, examples=True)
    # content_strategist does NOT grant calendar — only daily_briefing_agent does
    write_yaml(
        paths.workflows / "needs_calendar.yaml",
        {
            "id": "needs_calendar",
            "name": "Needs Calendar",
            "steps": [{"id": "list", "agent": "content_strategist", "action": "calendar.list_events"}],
        },
    )
    grant(paths, "content_strategist", "calendar.list_events")   # granted, but no calendar permission
    plan = plan_workflow(
        load_workflows(paths.workflows)["needs_calendar"],
        load_agents(paths.agents),
        registry=CapabilityRegistry.load(user_capabilities=tmp_path / "capabilities"),
    )
    assert plan["can_run"] is False
    assert plan["steps"][0]["policy"]["permission"] == "calendar.read"


def test_capability_memory_access_requires_memory_scope(tmp_path: Path, grant) -> None:
    # Build an agent with no memory_scope and a capability that declares memory.
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(
        paths.agents / "scopeless.yaml",
        {
            "id": "scopeless",
            "name": "Scopeless",
            "role": "test",
            "tools": ["summarize_day"],   # granted, but no memory_scope
            "permissions": {"calendar": "read"},
        },
    )
    write_yaml(
        paths.workflows / "needs_memory.yaml",
        {
            "id": "needs_memory",
            "name": "Needs Memory",
            "steps": [{"id": "summarize", "agent": "scopeless", "action": "summarize_day"}],
        },
    )
    plan = plan_workflow(
        load_workflows(paths.workflows)["needs_memory"],
        load_agents(paths.agents),
        registry=CapabilityRegistry.load(user_capabilities=paths.capabilities),
    )
    assert plan["can_run"] is False
    assert plan["steps"][0]["policy"]["permission"] == "memory.scope"


def test_capability_filesystem_permissions_are_checked_against_agent_policy(tmp_path: Path, grant) -> None:
    cap_dir = tmp_path / "capabilities" / "writer"
    cap_dir.mkdir(parents=True)
    write_yaml(
        cap_dir / "manifest.yaml",
        {
            "name": "writer",
            "version": "1.0.0",
            "summary": "Writes files.",
            "actions": ["writer.write"],
            "permissions": {"filesystem": {"write": ["/outside"]}},
            "risk_level": "low",
        },
    )
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(
        paths.workflows / "writer.yaml",
        {
            "id": "writer",
            "name": "Writer",
            "steps": [{"id": "write", "agent": "daily_briefing_agent", "action": "writer.write"}],
        },
    )
    grant(paths, "daily_briefing_agent", "writer.write")   # granted, but no filesystem permission
    plan = plan_workflow(
        load_workflows(paths.workflows)["writer"],
        load_agents(paths.agents),
        registry=CapabilityRegistry.load(user_capabilities=tmp_path / "capabilities"),
    )
    assert plan["can_run"] is False
    assert plan["steps"][0]["policy"]["permission"] == "filesystem.write"

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml
from jigga.core.models import WorkflowStep
from jigga.optional_capabilities import REGISTRY as OPTIONAL_CAPABILITIES
from jigga.runtime.capabilities import CapabilityManifest, CapabilityRegistry
from jigga.runtime.dispatcher import RuntimeContext, dispatch_action, evaluate_capability_permissions


@dataclass
class _Agent:
    id: str = "tester"
    role: str = "test"
    memory_scope: str | None = "task_only"
    permissions: dict | None = None


def _runtime(paths) -> RuntimeContext:
    return RuntimeContext(
        agent=_Agent(),
        home=paths.home,
        logs_dir=paths.logs,
        sessions_dir=paths.sessions,
    )


def _registry(paths) -> CapabilityRegistry:
    return CapabilityRegistry.load(user_capabilities=paths.capabilities)


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_dispatch_action_invokes_handler_without_a_workflow(tmp_path: Path) -> None:
    # This is the core PR C will call: invoke a capability action directly,
    # no workflow run_dir / outputs threading.
    paths = init_runtime(tmp_path, examples=True)
    step = WorkflowStep(id="tool_1", action="calendar.list_events", input={})
    output = dispatch_action(
        step,
        resolved_input={},
        memory_context={},
        runtime=_runtime(paths),
        registry=_registry(paths),
        logs_dir=paths.logs,
        run_id="agent_run_x",
    )
    # calendar.list_events is a bundled dry-run capability returning events.
    assert isinstance(output, list)
    assert output and output[0]["source"] == "capability.dry_run"


def test_dispatch_action_emits_invocation_events(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    step = WorkflowStep(id="tool_1", action="email.search", input={})
    dispatch_action(
        step,
        resolved_input={},
        memory_context={},
        runtime=_runtime(paths),
        registry=_registry(paths),
        logs_dir=paths.logs,
        run_id="agent_run_y",
    )
    types = [e["type"] for e in _events(paths)]
    assert "capability.invocation.started" in types
    assert "capability.invocation.completed" in types


def test_dispatch_action_raises_for_unknown_action(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    step = WorkflowStep(id="tool_1", action="does.not_exist", input={})
    with pytest.raises(ValueError, match="No capability registered"):
        dispatch_action(
            step,
            resolved_input={},
            memory_context={},
            runtime=_runtime(paths),
            registry=_registry(paths),
            logs_dir=paths.logs,
            run_id="agent_run_z",
        )


def test_dispatch_action_workflow_id_optional(tmp_path: Path) -> None:
    # Agent tool calls won't have a workflow_id; the started event should carry
    # workflow=None without error.
    paths = init_runtime(tmp_path, examples=True)
    step = WorkflowStep(id="tool_1", action="email.search", input={})
    dispatch_action(
        step,
        resolved_input={},
        memory_context={},
        runtime=_runtime(paths),
        registry=_registry(paths),
        logs_dir=paths.logs,
        run_id="r",
    )
    started = next(e for e in _events(paths) if e["type"] == "capability.invocation.started")
    assert started["details"]["workflow"] is None


def test_optional_capabilities_do_not_claim_unenforced_secret_permissions() -> None:
    for optional in OPTIONAL_CAPABILITIES.values():
        capability = CapabilityManifest.from_dict(read_yaml(optional.manifest_path))
        assert "secrets" not in capability.permissions


def test_secret_permissions_require_agent_grant() -> None:
    capability = CapabilityRegistry.load().resolve_action("calendar.list_events")
    capability = replace(capability, permissions={"secrets": {"required": ["TELEGRAM_BOT_TOKEN"]}})
    agent = _Agent(permissions={"network": {"mode": "allow"}})

    decision = evaluate_capability_permissions(capability, agent)

    assert decision.status == "deny"
    assert decision.permission == "secrets.TELEGRAM_BOT_TOKEN"


def test_secret_permissions_allow_named_secret() -> None:
    capability = CapabilityRegistry.load().resolve_action("calendar.list_events")
    capability = replace(capability, permissions={"secrets": {"required": ["TELEGRAM_BOT_TOKEN"]}})
    agent = _Agent(
        permissions={
            "network": {"mode": "allow"},
            "secrets": {"allow": ["TELEGRAM_BOT_TOKEN"]},
        }
    )

    decision = evaluate_capability_permissions(capability, agent)

    assert decision.status == "allow"

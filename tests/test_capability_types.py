from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.capabilities import (
    CapabilityManifest,
    CapabilityRegistry,
    load_capability_manifest,
    record_approval,
)
from jigga.runtime.workflow import run_workflow

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SKILL_PACK = REPO_ROOT / "examples" / "capabilities" / "skill-demo"
EXAMPLE_MCP_PACK = REPO_ROOT / "examples" / "capabilities" / "mcp-demo"


# --- Schema / validation ---------------------------------------------------


def _base_manifest(**overrides) -> dict:
    data = {
        "name": "demo",
        "version": "0.1.0",
        "summary": "demo",
        "actions": ["demo.x"],
    }
    data.update(overrides)
    return data


def test_default_type_is_native() -> None:
    capability = CapabilityManifest.from_dict(_base_manifest())
    assert capability.type == "native"
    # Native gets the dry_run.generic handler unless an explicit one is given.
    assert capability.handler == "dry_run.generic"


def test_invalid_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid capability type"):
        CapabilityManifest.from_dict(_base_manifest(type="webhook"))


def test_mcp_server_requires_command() -> None:
    with pytest.raises(ValueError, match="command"):
        CapabilityManifest.from_dict(_base_manifest(type="mcp_server"))


def test_skill_pack_defaults_instructions_filename() -> None:
    capability = CapabilityManifest.from_dict(_base_manifest(type="skill_pack"))
    assert capability.instructions == "instructions.md"
    # And gets the skill_pack default handler unless overridden.
    assert capability.handler == "skill_pack.default"


def test_mcp_server_defaults_handler_and_transport() -> None:
    capability = CapabilityManifest.from_dict(
        _base_manifest(type="mcp_server", command="python", args=["server.py"])
    )
    assert capability.handler == "mcp_server.subprocess"
    assert capability.transport == "stdio"
    assert capability.args == ["server.py"]


def test_explicit_handler_overrides_type_default() -> None:
    capability = CapabilityManifest.from_dict(
        _base_manifest(type="native", handler="dry_run.calendar")
    )
    assert capability.handler == "dry_run.calendar"


# --- Skill pack dispatch (end-to-end) --------------------------------------


def _copy_pack(source: Path, dest: Path) -> Path:
    shutil.copytree(source, dest)
    return dest


def test_skill_pack_dispatches_through_model_router(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    pack_dir = _copy_pack(EXAMPLE_SKILL_PACK, paths.capabilities / "skill-outline")
    record_approval(paths.policies, load_capability_manifest(pack_dir / "manifest.yaml"))

    write_yaml(
        paths.workflows / "outline.yaml",
        {
            "id": "outline",
            "name": "Outline",
            "steps": [
                {
                    "id": "draft",
                    "agent": "daily_briefing_agent",
                    "action": "skill.draft_outline",
                    "input": {"topic": "launch announcement"},
                }
            ],
        },
    )

    result = run_workflow(
        paths, "outline"
    )
    assert result["status"] == "completed"
    output = result["outputs"]["draft"]
    assert output["source"] == "capability.skill_pack"
    assert output["skill"] == "skill-outline"
    # Dry-run model provider returns a deterministic placeholder string.
    assert "Dry-run model response" in output["content"]


def test_skill_pack_missing_instructions_file_errors(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    cap_dir = paths.capabilities / "broken-skill"
    cap_dir.mkdir(parents=True)
    write_yaml(
        cap_dir / "manifest.yaml",
        {
            "name": "broken-skill",
            "version": "0.1.0",
            "summary": "Missing instructions file.",
            "type": "skill_pack",
            "actions": ["broken.run"],
            "instructions": "nope.md",
            "risk_level": "low",
        },
    )
    record_approval(paths.policies, load_capability_manifest(cap_dir / "manifest.yaml"))
    write_yaml(
        paths.workflows / "broken.yaml",
        {
            "id": "broken",
            "name": "Broken",
            "steps": [{"id": "x", "agent": "daily_briefing_agent", "action": "broken.run"}],
        },
    )
    with pytest.raises(ValueError, match="missing instructions"):
        run_workflow(
            paths, "broken"
        )


# --- MCP server dispatch (end-to-end against the demo server) --------------


def test_mcp_server_dispatches_against_demo_server(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    pack_dir = _copy_pack(EXAMPLE_MCP_PACK, paths.capabilities / "mcp-echo")
    # Rewrite manifest to use this venv's python so the test is portable, and
    # declare the capability as low-risk so the risk-level approval gate
    # doesn't short-circuit the dispatch path we want to exercise.
    manifest_path = pack_dir / "manifest.yaml"
    write_yaml(
        manifest_path,
        {
            "name": "mcp-echo",
            "version": "0.1.0",
            "summary": "Demo MCP echo server for tests.",
            "type": "mcp_server",
            "actions": ["demo.echo", "demo.upper"],
            "command": sys.executable,
            "args": ["server.py"],
            "risk_level": "low",
            # Self-restricting network declaration — capability doesn't use
            # the network at all (stdio subprocess only).
            "permissions": {"network": {"mode": "deny"}},
        },
    )
    record_approval(paths.policies, load_capability_manifest(manifest_path))

    write_yaml(
        paths.workflows / "mcp_echo.yaml",
        {
            "id": "mcp_echo",
            "name": "MCP Echo",
            "steps": [
                {
                    "id": "echo",
                    "agent": "daily_briefing_agent",
                    "action": "demo.echo",
                    "input": {"hello": "world"},
                }
            ],
        },
    )

    result = run_workflow(
        paths, "mcp_echo"
    )
    assert result["status"] == "completed", f"workflow did not complete: {result!r}"

    output = result["outputs"]["echo"]
    assert output["source"] == "capability.mcp_server"
    assert output["capability"] == "mcp-echo"
    # The demo server returns a content block containing the echoed args.
    text_blocks = output["result"]["content"]
    assert any("hello" in block.get("text", "") for block in text_blocks)


def test_capability_self_restricting_network_does_not_gate_agent() -> None:
    """A capability declaring `{network: {mode: deny}}` is self-restricting and
    must NOT force the executing agent's network mode to be open. This is the
    same polarity rule we apply to subagent deny rules."""
    from jigga.runtime.dispatcher import evaluate_capability_permissions
    from jigga.core.models import AgentConfig

    capability = CapabilityManifest.from_dict(
        _base_manifest(permissions={"network": {"mode": "deny"}})
    )
    agent_no_network = AgentConfig(
        id="a",
        name="A",
        role="r",
        memory_scope="task_only",
        permissions={"network": {"mode": "deny"}},
    )
    assert evaluate_capability_permissions(capability, agent_no_network).status == "allow"


def test_mcp_capability_appears_in_registry_with_typed_metadata(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    pack_dir = _copy_pack(EXAMPLE_MCP_PACK, paths.capabilities / "mcp-echo")
    record_approval(paths.policies, load_capability_manifest(pack_dir / "manifest.yaml"))
    registry = CapabilityRegistry.load(
        user_capabilities=paths.capabilities, approvals_dir=paths.policies
    )
    capability = registry.get("mcp-echo")
    assert capability is not None
    assert capability.type == "mcp_server"
    assert capability.handler == "mcp_server.subprocess"
    assert capability.command == "python"
    assert capability.args == ["server.py"]
    # to_index round-trips the new fields
    index = registry.to_index()
    entry = next(c for c in index["capabilities"] if c["name"] == "mcp-echo")
    assert entry["type"] == "mcp_server"
    assert entry["transport"] == "stdio"

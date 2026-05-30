from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.filesystem import (
    ACTION_HANDLERS,
    DEFAULT_SEARCH_MAX_MATCHES,
    MAX_LIST_ENTRIES,
    MAX_READ_BYTES,
    FilesystemPolicyError,
    filesystem_handler,
)
from jigga.runtime.workflow import run_workflow


@dataclass
class _StubRuntime:
    """Minimal RuntimeContext stand-in for unit-testing the handler in
    isolation. The real one carries home/logs_dir/sessions_dir/agent; the
    filesystem handler only reads `agent`."""

    agent: AgentConfig | None


def _agent_with_allow(workspace: Path, *, write: bool = False) -> AgentConfig:
    fs = {"allow": [str(workspace)]}
    if write:
        fs["deny"] = []
    return AgentConfig(
        id="agent_x",
        name="Agent X",
        role="filesystem test",
        memory_scope="task_only",
        permissions={"filesystem": fs},
    )


def _agent_no_filesystem() -> AgentConfig:
    return AgentConfig(
        id="agent_locked",
        name="Agent Locked",
        role="no filesystem perms",
        memory_scope="task_only",
        permissions={},
    )


def _step(action: str, input_dict: dict[str, Any] | None = None) -> WorkflowStep:
    return WorkflowStep(id="t", action=action, input=input_dict or {})


def _call(action: str, runtime: _StubRuntime, input_dict: dict[str, Any]) -> Any:
    return filesystem_handler(
        _step(action, input_dict), capability=None, resolved_input=input_dict,
        memory_context={}, runtime=runtime,
    )


# --- read_file --------------------------------------------------------------


def test_read_file_returns_content_for_allowed_path(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("hi there", encoding="utf-8")
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    result = _call("filesystem.read_file", runtime, {"path": str(target)})
    assert result["exists"] is True
    assert result["content"] == "hi there"
    assert result["size"] == len(b"hi there")
    assert result["source"] == "capability.filesystem"


def test_read_file_reports_missing_without_raising(tmp_path: Path) -> None:
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    result = _call("filesystem.read_file", runtime, {"path": str(tmp_path / "nope.txt")})
    assert result["exists"] is False
    assert result["content"] is None


def test_read_file_denied_outside_agent_allow_list(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    runtime = _StubRuntime(agent=_agent_with_allow(workspace))
    with pytest.raises(FilesystemPolicyError, match="cannot read"):
        _call("filesystem.read_file", runtime, {"path": str(outside)})


def test_read_file_caps_oversized_content(tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    # Write just over the cap. Using "0" bytes keeps the test fast.
    big.write_bytes(b"0" * (MAX_READ_BYTES + 10))
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    with pytest.raises(ValueError, match="exceeds cap"):
        _call("filesystem.read_file", runtime, {"path": str(big)})


def test_read_file_rejects_when_target_is_directory(tmp_path: Path) -> None:
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    with pytest.raises(ValueError, match="is a directory"):
        _call("filesystem.read_file", runtime, {"path": str(tmp_path)})


def test_read_file_requires_agent_with_filesystem_perms(tmp_path: Path) -> None:
    target = tmp_path / "hi.txt"
    target.write_text("hi", encoding="utf-8")
    runtime = _StubRuntime(agent=_agent_no_filesystem())
    with pytest.raises(FilesystemPolicyError):
        _call("filesystem.read_file", runtime, {"path": str(target)})


# --- write_file -------------------------------------------------------------


def test_write_file_writes_content_to_allowed_path(tmp_path: Path) -> None:
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    target = tmp_path / "out.md"
    result = _call("filesystem.write_file", runtime, {"path": str(target), "content": "hello"})
    assert result["bytes_written"] == len("hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_write_file_refuses_existing_without_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "out.md"
    target.write_text("original", encoding="utf-8")
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    with pytest.raises(ValueError, match="overwrite: true"):
        _call("filesystem.write_file", runtime, {"path": str(target), "content": "new"})
    assert target.read_text(encoding="utf-8") == "original"


def test_write_file_overwrites_when_opted_in(tmp_path: Path) -> None:
    target = tmp_path / "out.md"
    target.write_text("original", encoding="utf-8")
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    _call(
        "filesystem.write_file",
        runtime,
        {"path": str(target), "content": "new", "overwrite": True},
    )
    assert target.read_text(encoding="utf-8") == "new"


def test_write_file_creates_parents_only_when_opted_in(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "out.md"
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    with pytest.raises(ValueError, match="parent directory"):
        _call("filesystem.write_file", runtime, {"path": str(target), "content": "x"})
    _call(
        "filesystem.write_file",
        runtime,
        {"path": str(target), "content": "x", "create_parents": True},
    )
    assert target.read_text(encoding="utf-8") == "x"


def test_write_file_denied_outside_allow_list(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "rogue.txt"
    runtime = _StubRuntime(agent=_agent_with_allow(workspace))
    with pytest.raises(FilesystemPolicyError):
        _call("filesystem.write_file", runtime, {"path": str(outside), "content": "no"})


def test_write_file_coerces_non_string_content(tmp_path: Path) -> None:
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    target = tmp_path / "n.txt"
    result = _call("filesystem.write_file", runtime, {"path": str(target), "content": 42})
    assert target.read_text(encoding="utf-8") == "42"
    assert result["bytes_written"] == 2


# --- list_directory ---------------------------------------------------------


def test_list_directory_returns_immediate_entries(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "b.md").write_text("b", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    result = _call("filesystem.list_directory", runtime, {"path": str(tmp_path)})
    names = {entry["name"] for entry in result["entries"]}
    assert names == {"a.md", "b.md", "sub"}
    sub_entry = next(e for e in result["entries"] if e["name"] == "sub")
    assert sub_entry["is_dir"] is True
    assert sub_entry["size"] is None


def test_list_directory_recursive_with_glob(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("b", encoding="utf-8")
    (tmp_path / "sub" / "c.txt").write_text("c", encoding="utf-8")
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    result = _call(
        "filesystem.list_directory",
        runtime,
        {"path": str(tmp_path), "recursive": True, "glob": "*.md"},
    )
    md_entries = {entry["name"] for entry in result["entries"]}
    assert md_entries == {"a.md", "b.md"}


def test_list_directory_caps_at_max_entries(tmp_path: Path) -> None:
    # Create just over the cap so iteration truncates rather than running long.
    for i in range(MAX_LIST_ENTRIES + 5):
        (tmp_path / f"f_{i}.txt").write_text("x", encoding="utf-8")
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    result = _call("filesystem.list_directory", runtime, {"path": str(tmp_path)})
    assert len(result["entries"]) == MAX_LIST_ENTRIES
    assert result["truncated"] is True


def test_list_directory_denied_outside_allow_list(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime = _StubRuntime(agent=_agent_with_allow(workspace))
    with pytest.raises(FilesystemPolicyError):
        _call("filesystem.list_directory", runtime, {"path": str(outside)})


# --- search_files -----------------------------------------------------------


def test_search_files_finds_matches_across_files(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("hello world\nnothing\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("nothing here\nHELLO again\n", encoding="utf-8")
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    result = _call(
        "filesystem.search_files",
        runtime,
        {"path": str(tmp_path), "pattern": "hello"},
    )
    paths = {match["path"] for match in result["matches"]}
    assert paths == {str(tmp_path / "a.md"), str(tmp_path / "b.md")}
    # Case-insensitive by default — "HELLO again" was matched
    assert any("HELLO again" in m["line"] for m in result["matches"])


def test_search_files_case_sensitive_excludes_uppercase(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("HELLO\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("hello\n", encoding="utf-8")
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    result = _call(
        "filesystem.search_files",
        runtime,
        {"path": str(tmp_path), "pattern": "hello", "case_sensitive": True},
    )
    paths = {match["path"] for match in result["matches"]}
    assert paths == {str(tmp_path / "b.md")}


def test_search_files_respects_max_matches(tmp_path: Path) -> None:
    target = tmp_path / "f.md"
    target.write_text("hit\n" * 10, encoding="utf-8")
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    result = _call(
        "filesystem.search_files",
        runtime,
        {"path": str(tmp_path), "pattern": "hit", "max_matches": 3},
    )
    assert len(result["matches"]) == 3
    assert result["truncated"] is True


def test_search_files_rejects_invalid_regex(tmp_path: Path) -> None:
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    with pytest.raises(ValueError, match="invalid regex"):
        _call(
            "filesystem.search_files",
            runtime,
            {"path": str(tmp_path), "pattern": "[unclosed"},
        )


def test_search_files_denied_outside_allow_list(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime = _StubRuntime(agent=_agent_with_allow(workspace))
    with pytest.raises(FilesystemPolicyError):
        _call(
            "filesystem.search_files",
            runtime,
            {"path": str(outside), "pattern": "x"},
        )


# --- handler dispatch + capability registration ----------------------------


def test_handler_rejects_unknown_action(tmp_path: Path) -> None:
    runtime = _StubRuntime(agent=_agent_with_allow(tmp_path))
    with pytest.raises(ValueError, match="Unknown filesystem action"):
        filesystem_handler(
            _step("filesystem.delete_everything"),
            capability=None,
            resolved_input={"path": str(tmp_path)},
            memory_context={},
            runtime=runtime,
        )


def test_action_handlers_table_covers_documented_actions() -> None:
    assert set(ACTION_HANDLERS) == {
        "filesystem.read_file",
        "filesystem.write_file",
        "filesystem.list_directory",
        "filesystem.search_files",
    }


def test_bundled_filesystem_capability_resolves_each_action() -> None:
    registry = CapabilityRegistry.load()
    capability = registry.get("filesystem")
    assert capability is not None
    assert capability.handler == "runtime.filesystem"
    assert capability.risk_level == "low"
    for action in (
        "filesystem.read_file",
        "filesystem.write_file",
        "filesystem.list_directory",
        "filesystem.search_files",
    ):
        assert registry.resolve_action(action) is capability


# --- workflow integration ---------------------------------------------------


def test_workflow_round_trip_write_then_read(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    # The bundled daily_briefing_agent allows ~/.jigga/memory/summaries; tests
    # use a tmp_path runtime, so we widen the agent's allow list to include
    # this test's runtime home. This mirrors the real user pattern: pick an
    # agent, point it at the workspace it should be able to touch.
    target = paths.memory / "summaries" / "status.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    agent_yaml = paths.agents / "daily_briefing_agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text(encoding="utf-8").replace(
            "- ~/.jigga/memory/summaries",
            f"- ~/.jigga/memory/summaries\n      - {paths.home}",
        ),
        encoding="utf-8",
    )
    write_yaml(
        paths.workflows / "rw.yaml",
        {
            "id": "rw",
            "name": "Round trip",
            "steps": [
                {
                    "id": "write",
                    "agent": "daily_briefing_agent",
                    "action": "filesystem.write_file",
                    "input": {
                        "path": str(target),
                        "content": "ready",
                        "overwrite": True,
                        "create_parents": True,
                    },
                },
                {
                    "id": "read",
                    "agent": "daily_briefing_agent",
                    "action": "filesystem.read_file",
                    "input": {"path": str(target)},
                },
            ],
        },
    )
    result = run_workflow(
        paths.home, paths.logs, paths.workflows, paths.agents, paths.memory, "rw"
    )
    assert result["status"] == "completed"
    assert result["outputs"]["read"]["content"] == "ready"


def test_workflow_filesystem_denied_path_blocks_at_runtime(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    # daily_briefing_agent has no read access to /etc/passwd.
    write_yaml(
        paths.workflows / "rogue.yaml",
        {
            "id": "rogue",
            "name": "Rogue",
            "steps": [
                {
                    "id": "read",
                    "agent": "daily_briefing_agent",
                    "action": "filesystem.read_file",
                    "input": {"path": "/etc/passwd"},
                }
            ],
        },
    )
    # The capability declares no static paths so plan-time passes; the runtime
    # check fires inside the handler and raises FilesystemPolicyError, which
    # the workflow runner currently surfaces as an exception. Future work
    # could catch this and mark the step needs_approval instead.
    with pytest.raises(FilesystemPolicyError):
        run_workflow(
            paths.home, paths.logs, paths.workflows, paths.agents, paths.memory, "rogue"
        )


def test_filesystem_capability_round_trips_in_cli_index() -> None:
    # Sanity check that `jigga capabilities list` would show the new bundle.
    registry = CapabilityRegistry.load()
    index = registry.to_index()
    fs_actions = [action for action in index["actions"] if action.startswith("filesystem.")]
    assert set(fs_actions) == {
        "filesystem.read_file",
        "filesystem.write_file",
        "filesystem.list_directory",
        "filesystem.search_files",
    }
    fs_entry = next(c for c in index["capabilities"] if c["name"] == "filesystem")
    assert fs_entry["handler"] == "runtime.filesystem"


# --- constants pinned to catch silent loosening ----------------------------


def test_caps_are_pinned() -> None:
    # If you raise these caps, do it deliberately — they're the difference
    # between "safe to ship bundled" and "could load 50MB into a run dict".
    assert MAX_READ_BYTES == 1_048_576
    assert MAX_LIST_ENTRIES == 1_000
    assert DEFAULT_SEARCH_MAX_MATCHES == 100

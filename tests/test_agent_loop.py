from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.agent import _to_tool_name, run_agent
from jigga.runtime.model_router import ModelCallResult, ModelToolCall
from jigga.runtime.tasks import create_task, list_tasks


def _result(content: str = "", tool_calls=None) -> ModelCallResult:
    return ModelCallResult(
        status="ok", provider="dry_run", model="m", content=content, dry_run=True,
        tool_calls=tool_calls or [],
    )


def _scripted_call_model(sequence):
    it = iter(sequence)

    def fake(home, logs_dir, request):
        return next(it)

    return fake


def _write_agent(paths, agent_id, tools, *, permission_mode=None, delegation=None) -> None:
    data = {
        "id": agent_id,
        "name": agent_id,
        "role": "tester",
        "memory_scope": "task_only",
        "tools": tools,
        "permissions": {},
    }
    if permission_mode:
        data["permission_mode"] = permission_mode
    if delegation:
        data["delegation"] = delegation
    write_yaml(paths.agents / f"{agent_id}.yaml", data)


def _set_loop_limits(paths, *, max_tool_calls=None, max_iterations=None) -> None:
    config = read_yaml(paths.config)
    loop = {}
    if max_tool_calls is not None:
        loop["max_tool_calls_per_run"] = max_tool_calls
    if max_iterations is not None:
        loop["max_iterations"] = max_iterations
    config["agent_loop"] = loop
    write_yaml(paths.config, config)


def _events(paths):
    path = paths.logs / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _types(paths):
    return [e["type"] for e in _events(paths)]


# --- name sanitization -----------------------------------------------------


def test_tool_name_sanitization_roundtrips() -> None:
    assert _to_tool_name("telegram.send_message") == "telegram__send_message"
    assert _to_tool_name("gog.gmail_search") == "gog__gmail_search"
    # single underscores untouched
    assert _to_tool_name("summarize_day") == "summarize_day"


# --- happy path: real capability dispatch ----------------------------------


def test_agent_calls_real_capability_then_finishes(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    create_task(paths.tasks, "Check calendar", assignee="daily_briefing_agent")
    sequence = [
        _result(tool_calls=[ModelToolCall(id="c1", name=_to_tool_name("calendar.list_events"), arguments={})]),
        _result(content="Found 2 events."),
    ]
    with patch("jigga.runtime.agent.call_model", _scripted_call_model(sequence)):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "daily_briefing_agent")

    assert list_tasks(paths.tasks)[0].state == "completed"
    types = _types(paths)
    assert "agent.tool_call.requested" in types
    assert "agent.tool_call.executed" in types
    assert "capability.invocation.started" in types  # the real dispatch fired
    # artifact records the final text + the tool call made
    run_files = list((paths.home / "runs" / "agents" / "daily_briefing_agent").rglob("*.json"))
    artifact = next(json.loads(f.read_text()) for f in run_files if f.name.startswith("task_"))
    assert artifact["result"] == "Found 2 events."
    assert artifact["tool_calls"][0]["action"] == "calendar.list_events"


# --- allowlist denial ------------------------------------------------------


def test_uncovered_action_is_denied(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_agent(paths, "narrow", tools=["calendar.list_events"])
    create_task(paths.tasks, "do", assignee="narrow")
    # Model tries to call email.search which is NOT in the agent's tools.
    sequence = [
        _result(tool_calls=[ModelToolCall(id="c1", name=_to_tool_name("email.search"), arguments={})]),
        _result(content="ok done"),
    ]
    with patch("jigga.runtime.agent.call_model", _scripted_call_model(sequence)):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "narrow")
    denied = [e for e in _events(paths) if e["type"] == "agent.tool_call.denied"]
    assert denied
    assert "allowlist" in denied[-1]["details"]["reason"]
    # task still completes after the model's final text turn
    assert list_tasks(paths.tasks)[0].state == "completed"


# --- risk gating -----------------------------------------------------------


def test_medium_risk_tool_halts_for_approval_under_ask_mode(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    # subagent-delegation is a bundled medium-risk capability (spawn_subagent).
    _write_agent(paths, "delegator", tools=["spawn_subagent"])  # default mode = ask
    create_task(paths.tasks, "delegate something", assignee="delegator")
    sequence = [
        _result(tool_calls=[ModelToolCall(id="c1", name="spawn_subagent", arguments={"work_order": {"goal": "x"}})]),
    ]
    with patch("jigga.runtime.agent.call_model", _scripted_call_model(sequence)):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "delegator")
    types = _types(paths)
    assert "agent.tool_call.needs_approval" in types
    assert list_tasks(paths.tasks)[0].state == "needs_approval"


def test_autonomous_mode_proceeds_past_risk_gate(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_agent(paths, "auto", tools=["spawn_subagent"], permission_mode="autonomous")
    create_task(paths.tasks, "delegate something", assignee="auto")
    sequence = [
        _result(tool_calls=[ModelToolCall(id="c1", name="spawn_subagent", arguments={})]),
        _result(content="delegated."),
    ]
    # Stub dispatch so we test the gate decision, not spawn_subagent internals.
    with patch("jigga.runtime.agent.call_model", _scripted_call_model(sequence)), patch(
        "jigga.runtime.agent.dispatch_action", return_value={"ok": True}
    ):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "auto")
    types = _types(paths)
    assert "agent.tool_call.executed" in types
    assert "agent.tool_call.needs_approval" not in types
    assert list_tasks(paths.tasks)[0].state == "completed"


# --- bounds ----------------------------------------------------------------


def test_max_tool_calls_per_run_halts(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    # spawn_subagent's capability permission (delegation) is ignored by the
    # resource-permission gate, so only the risk gate applies — under
    # autonomous mode it executes, letting us exercise the call cap cleanly.
    _write_agent(paths, "looper", tools=["spawn_subagent"], permission_mode="autonomous")
    _set_loop_limits(paths, max_tool_calls=1)
    create_task(paths.tasks, "loop", assignee="looper")
    call = ModelToolCall(id="c", name="spawn_subagent", arguments={})
    sequence = [
        _result(tool_calls=[call]),   # call #1 → executes (calls_made=1)
        _result(tool_calls=[call]),   # call #2 → over the cap → denied + halt
    ]
    with patch("jigga.runtime.agent.call_model", _scripted_call_model(sequence)), patch(
        "jigga.runtime.agent.dispatch_action", return_value={"ok": True}
    ):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "looper")
    denied = [e for e in _events(paths) if e["type"] == "agent.tool_call.denied"]
    assert any("max_tool_calls" in d["details"].get("reason", "") for d in denied)


def test_max_iterations_halt_is_explicit(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_agent(paths, "looper", tools=["calendar.list_events"])
    _set_loop_limits(paths, max_iterations=1)
    create_task(paths.tasks, "loop", assignee="looper")
    sequence = [_result(tool_calls=[ModelToolCall(id="c1", name=_to_tool_name("calendar.list_events"), arguments={})])]
    with patch("jigga.runtime.agent.call_model", _scripted_call_model(sequence)):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "looper")

    assert list_tasks(paths.tasks)[0].state == "completed"
    halted = [e for e in _events(paths) if e["type"] == "agent.loop.halted"]
    assert halted
    assert halted[-1]["details"]["reason"] == "max_iterations=1"
    run_files = list((paths.home / "runs" / "agents" / "looper").rglob("*.json"))
    artifact = next(json.loads(f.read_text()) for f in run_files if f.name.startswith("task_"))
    assert artifact["halted"] == {"reason": "max_iterations=1"}


# --- no-tool path preserved ------------------------------------------------


def test_agent_with_no_tools_completes_in_one_turn(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_agent(paths, "plain", tools=[])
    create_task(paths.tasks, "summarize", assignee="plain")
    sequence = [_result(content="here is your summary")]
    with patch("jigga.runtime.agent.call_model", _scripted_call_model(sequence)):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "plain")
    assert list_tasks(paths.tasks)[0].state == "completed"
    types = _types(paths)
    assert "agent.tool_call.requested" not in types  # no tools → no tool calls


def test_model_failure_marks_task_failed(tmp_path: Path) -> None:
    """When the model call errors, the task must land in 'failed' — not silently
    'completed'. This is the agent loop's error branch (agent.py: status != ok)."""
    paths = init_runtime(tmp_path)
    _write_agent(paths, "worker", tools=[])
    create_task(paths.tasks, "do work", assignee="worker")
    failing = ModelCallResult(status="error", provider="p", model="m", content="",
                              dry_run=True, error="provider exploded")
    with patch("jigga.runtime.agent.call_model", _scripted_call_model([failing])):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "worker")
    assert list_tasks(paths.tasks)[0].state == "failed"

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.config import load_agents
from jigga.core.io import write_yaml
from jigga.runtime.agent import run_agent
from jigga.runtime.audit import (
    append_event,
    current_trace_id,
    trace_context,
)
from jigga.runtime.audit_query import trace
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.subagents import spawn_subagent
from jigga.runtime.supervisor import supervisor_tick
from jigga.runtime.tasks import create_task


def _events(logs: Path) -> list[dict]:
    path = logs / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _no_tool_result(home, logs_dir, request) -> ModelCallResult:
    return ModelCallResult(status="ok", provider="dry_run", model="m", content="done", dry_run=True, tool_calls=[])


def _write_agent(paths, agent_id: str) -> None:
    write_yaml(paths.agents / f"{agent_id}.yaml", {
        "id": agent_id, "name": agent_id, "role": "tester",
        "memory_scope": "task_only", "tools": [], "permissions": {},
    })


# --- trace_context unit ----------------------------------------------------


def test_trace_context_mints_and_attaches(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    with trace_context() as tid:
        assert tid.startswith("trace_")
        event = append_event(logs, "x.y")
    assert event["details"]["trace_id"] == tid


def test_trace_context_nested_inherits() -> None:
    with trace_context() as outer:
        with trace_context() as inner:
            assert inner == outer
            assert current_trace_id() == outer
        # inner reset does not clear the outer trace
        assert current_trace_id() == outer
    assert current_trace_id() is None


def test_trace_context_explicit_id_wins() -> None:
    with trace_context("trace_fixed") as tid:
        assert tid == "trace_fixed"
        assert current_trace_id() == "trace_fixed"


def test_no_trace_id_outside_a_context(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    event = append_event(logs, "x.y")
    assert "trace_id" not in event["details"]
    assert current_trace_id() is None


def test_explicit_trace_id_detail_is_not_overwritten(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    with trace_context("trace_ctx"):
        event = append_event(logs, "x.y", trace_id="trace_explicit")
    assert event["details"]["trace_id"] == "trace_explicit"


# --- propagation across an agent run ---------------------------------------


def test_run_agent_events_share_one_trace(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _write_agent(paths, "solo")
    create_task(paths.tasks, "do the thing", assignee="solo")
    with patch("jigga.runtime.agent.call_model", _no_tool_result):
        record = run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "solo")

    tid = record["trace_id"]
    assert tid and tid.startswith("trace_")
    run_events = [e for e in _events(paths.logs) if e["details"].get("trace_id") == tid]
    assert {"agent.run.started", "agent.run.completed"} <= {e["type"] for e in run_events}
    # one id returns the whole run, and nothing leaks in without the trace
    traced = trace(paths.logs, tid)
    assert traced
    assert all(e["details"].get("trace_id") == tid for e in traced)


# --- propagation from supervisor tick down to the agent run ----------------


def test_supervisor_tick_trace_spans_the_agent_run(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _write_agent(paths, "ticker")
    create_task(paths.tasks, "wake work", assignee="ticker")
    with patch("jigga.runtime.agent.call_model", _no_tool_result):
        supervisor_tick(paths.home)

    events = _events(paths.logs)
    tick = next(e for e in events if e["type"] == "supervisor.tick")
    tid = tick["details"]["trace_id"]
    run_started = next(e for e in events if e["type"] == "agent.run.started")
    # the agent run the tick triggered shares the tick's trace
    assert run_started["details"]["trace_id"] == tid
    # one id returns both the tick and the run it caused
    traced_types = {e["type"] for e in trace(paths.logs, tid)}
    assert {"supervisor.tick", "agent.run.started", "agent.run.completed"} <= traced_types


# --- propagation into a spawned subagent -----------------------------------


def test_subagent_spawn_inherits_the_active_trace(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = load_agents(paths.agents)["content_strategist"]
    payload = {
        "backend": "dry_run",
        "mode": "plan",
        "parent_agent_id": "content_strategist",
        "task_id": "task_demo",
        "work_order": {"goal": "Inspect the content plan"},
        "cwd": "~/Projects/content",
    }
    with trace_context("trace_lead"):
        spawn_subagent(paths.home, paths.logs, paths.sessions, agent, payload)

    spawn_events = [e for e in _events(paths.logs) if e["type"].startswith("subagent.spawn")]
    assert spawn_events
    assert all(e["details"]["trace_id"] == "trace_lead" for e in spawn_events)

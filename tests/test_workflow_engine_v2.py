"""Workflow engine v2 (DAG runner): graph validation, branching, resumable
persisted runs, human_approval parking via the shared approval queue, and the
supervisor heartbeat advancing non-terminal runs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_workflows
from jigga.core.io import read_json
from jigga.runtime import workflow_engine as engine
from jigga.runtime.approvals import pending_approvals, resolve, resolve_and_requeue
from jigga.runtime.supervisor import supervisor_tick
from jigga.runtime.workflow import plan_workflow, run_workflow
from jigga.runtime.workflow_engine import (
    advance_all_runs,
    advance_run,
    alarm_stale_approvals,
    list_runs,
    load_run,
    start_run,
    validate_graph,
)


def _events(paths) -> list[dict[str, Any]]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _write_workflow(paths, doc: dict[str, Any]) -> None:
    (paths.workflows / f"{doc['id']}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


# The example home scaffolds this agent; tool/llm nodes need an executing agent
# (same policy as v1 steps).
AGENT = "daily_briefing_agent"


def _dag(workflow_id: str = "v2_demo", **overrides: Any) -> dict[str, Any]:
    """A two-node linear DAG using example-home capabilities (calendar → notify)."""
    doc = {
        "id": workflow_id,
        "name": "V2 demo",
        "status": "active",
        "nodes": [
            {"id": "read_calendar", "agent": AGENT, "action": "calendar.list_events",
             "input": {"range": "today"}},
            {"id": "notify", "agent": AGENT, "action": "notifications.send",
             "input": {"message": "done"}},
        ],
        "edges": [{"from": "read_calendar", "to": "notify"}],
    }
    doc.update(overrides)
    return doc


# --- graph validation -------------------------------------------------------


def test_validate_graph_catches_structural_problems(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, {
        "id": "broken",
        "name": "Broken",
        "nodes": [
            {"id": "a", "action": "calendar.list_events"},
            {"id": "a", "action": "calendar.list_events"},
            {"id": "b", "type": "mystery"},
            {"id": "c", "type": "tool"},
            {"id": "d", "type": "writeback"},
        ],
        "edges": [
            {"from": "a", "to": "missing"},
            {"from": "a", "to": "c", "on": "sometimes"},
        ],
    })
    problems = validate_graph(load_workflows(paths.workflows)["broken"])
    text = "\n".join(problems)
    assert "duplicate node id: a" in text
    assert "unknown type 'mystery'" in text
    assert "tool node requires an action" in text
    assert "writeback node requires input.path" in text
    assert "unknown node 'missing'" in text
    assert "on='sometimes'" in text


def test_validate_graph_catches_cycles_and_validate_surfaces_them(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, {
        "id": "loopy",
        "name": "Loopy",
        "nodes": [{"id": "a", "action": "calendar.list_events"},
                  {"id": "b", "action": "calendar.list_events"}],
        "edges": [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
    })
    assert "workflow graph has a cycle" in validate_graph(load_workflows(paths.workflows)["loopy"])
    from jigga.runtime.plan_apply import validate_runtime_configs

    problems = validate_runtime_configs(paths)["problems"]
    assert any("loopy" in p and "cycle" in p for p in problems)


# --- linear + branching runs ------------------------------------------------


def test_linear_dag_runs_to_completion(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _dag())
    result = run_workflow(paths, "v2_demo")
    assert result["engine"] == "v2"
    assert result["status"] == "completed"
    assert result["nodes"]["read_calendar"]["status"] == "done"
    assert result["nodes"]["notify"]["status"] == "done"
    assert "read_calendar" in result["outputs"]
    on_disk = read_json(Path(result["run_dir"]) / "run.json")
    assert on_disk["status"] == "completed"


def test_error_edge_takes_failure_branch_and_skips_success_branch(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, {
        "id": "branchy",
        "name": "Branchy",
        "nodes": [
            # Writeback escaping workspaces/ raises -> the node fails.
            {"id": "bad", "type": "writeback", "input": {"value": "x", "path": "workspaces/../evil.txt"}},
            {"id": "on_ok", "agent": AGENT, "action": "notifications.send", "input": {"message": "ok"}},
            {"id": "on_err", "agent": AGENT, "action": "notifications.send", "input": {"message": "recovered"}},
        ],
        "edges": [
            {"from": "bad", "to": "on_ok", "on": "success"},
            {"from": "bad", "to": "on_err", "on": "error"},
        ],
    })
    result = run_workflow(paths, "branchy")
    assert result["nodes"]["bad"]["status"] == "failed"
    assert result["nodes"]["on_err"]["status"] == "done"
    assert result["nodes"]["on_ok"]["status"] == "skipped"
    # The failure was handled by the error edge, so the run completes.
    assert result["status"] == "completed"
    assert not (paths.home / "evil.txt").exists()


def test_unhandled_node_failure_fails_the_run(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _dag("no_cap", nodes=[
        {"id": "nope", "agent": AGENT, "action": "nothing.registered"},
    ], edges=[]))
    result = run_workflow(paths, "no_cap")
    assert result["status"] == "failed"
    assert result["nodes"]["nope"]["status"] == "failed"
    assert "unhandled node failure: nope" in result["error"]


def test_optional_node_failure_is_tolerated(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _dag("optional_fail", nodes=[
        {"id": "nope", "agent": AGENT, "action": "nothing.registered", "optional": True},
        {"id": "notify", "agent": AGENT, "action": "notifications.send", "input": {"message": "done"}},
    ], edges=[{"from": "nope", "to": "notify", "on": "always"}]))
    result = run_workflow(paths, "optional_fail")
    assert result["nodes"]["nope"]["status"] == "failed"
    assert result["nodes"]["notify"]["status"] == "done"
    assert result["status"] == "completed"


def test_llm_node_drafts_via_model(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _dag("llm_flow", nodes=[
        {"id": "think", "type": "llm", "agent": AGENT,
         "input": {"prompt": "Summarize the day"}, "output": "summary.md"},
    ], edges=[]))
    result = run_workflow(paths, "llm_flow")
    assert result["status"] == "completed"
    assert isinstance(result["outputs"]["think"], str) and result["outputs"]["think"]
    assert (Path(result["run_dir"]) / "summary.md").exists()


def test_writeback_writes_upstream_output_into_workspaces(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _dag("wb", nodes=[
        {"id": "read_calendar", "agent": AGENT, "action": "calendar.list_events", "output": "events"},
        {"id": "save", "type": "writeback",
         "input": {"source": "events", "path": "workspaces/demo/events.json"}},
    ], edges=[{"from": "read_calendar", "to": "save"}]))
    result = run_workflow(paths, "wb")
    assert result["status"] == "completed"
    saved = read_json(paths.home / "workspaces" / "demo" / "events.json")
    assert saved == result["outputs"]["events"]


# --- resumability -----------------------------------------------------------


def test_run_resumes_from_disk_after_restart(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _dag())
    workflow = load_workflows(paths.workflows)["v2_demo"]
    record = start_run(paths, workflow)
    advance_run(paths, record, max_nodes=1)
    assert record["nodes"]["read_calendar"]["status"] == "done"
    assert record["nodes"]["notify"]["status"] == "pending"
    assert record["status"] == "running"
    # "Restart": reload purely from disk and advance again.
    reloaded = load_run(paths, record["id"])
    assert reloaded is not None and reloaded["status"] == "running"
    finished = advance_run(paths, reloaded)
    assert finished["status"] == "completed"
    assert finished["nodes"]["notify"]["status"] == "done"


# --- human approval ---------------------------------------------------------


def _approval_dag(workflow_id: str = "gated") -> dict[str, Any]:
    return {
        "id": workflow_id,
        "name": "Gated",
        "nodes": [
            {"id": "gate", "type": "human_approval", "input": {"prompt": "Ship it?"}},
            {"id": "ship", "agent": AGENT, "action": "notifications.send", "input": {"message": "shipped"}},
            {"id": "abort", "agent": AGENT, "action": "notifications.send", "input": {"message": "aborted"}},
        ],
        "edges": [
            {"from": "gate", "to": "ship", "on": "success"},
            {"from": "gate", "to": "abort", "on": "error"},
        ],
    }


def test_human_approval_parks_then_approve_resumes(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _approval_dag())
    result = run_workflow(paths, "gated")
    assert result["status"] == "awaiting_approval"
    assert result["nodes"]["gate"]["status"] == "awaiting_approval"
    pending = pending_approvals(paths.approvals)
    assert len(pending) == 1
    assert pending[0]["code"] == result["nodes"]["gate"]["approval_code"]

    # `approve <code>` arrives via the channel gateway path (resolve_and_requeue
    # must tolerate the synthetic wfrun task id) — then the next advance resumes.
    assert resolve_and_requeue(paths.approvals, paths.tasks, pending[0]["code"], approved=True) is not None
    resumed = advance_run(paths, load_run(paths, result["id"]))
    assert resumed["status"] == "completed"
    assert resumed["nodes"]["ship"]["status"] == "done"
    assert resumed["nodes"]["abort"]["status"] == "skipped"
    assert resumed["outputs"]["gate"] == {"approved": True}


def test_human_approval_denied_takes_error_branch(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _approval_dag("gated_deny"))
    result = run_workflow(paths, "gated_deny")
    code = result["nodes"]["gate"]["approval_code"]
    assert resolve(paths.approvals, code, approved=False) is not None
    resumed = advance_run(paths, load_run(paths, result["id"]))
    assert resumed["nodes"]["gate"]["status"] == "failed"
    assert resumed["nodes"]["abort"]["status"] == "done"
    assert resumed["nodes"]["ship"]["status"] == "skipped"
    assert resumed["status"] == "completed"  # denial handled by the error edge


def test_supervisor_tick_advances_approved_run(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _approval_dag("gated_tick"))
    result = run_workflow(paths, "gated_tick")
    code = result["nodes"]["gate"]["approval_code"]
    resolve(paths.approvals, code, approved=True)
    supervisor_tick(paths.home)
    record = load_run(paths, result["id"])
    assert record["status"] == "completed"
    assert record["nodes"]["ship"]["status"] == "done"


def test_advance_all_runs_reports_and_bounds(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _approval_dag("gated_all"))
    parked = run_workflow(paths, "gated_all")
    summary = advance_all_runs(paths)
    assert summary["active"] == 1
    assert summary["advanced"] == []  # still parked; nothing changed
    resolve(paths.approvals, parked["nodes"]["gate"]["approval_code"], approved=True)
    summary = advance_all_runs(paths)
    assert [entry["status"] for entry in summary["advanced"]] == ["completed"]


# --- approval deliverability + staleness ------------------------------------
# The precursor stack parked a run for 36 days because an approval targeting an
# unconfigured channel was undeliverable, and "never asked" looked exactly like
# "not answered yet". These pin the two halves that break that tie.


class _Adapter:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []

    def send(self, home, *, conversation_id, text) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("bot token rejected")
        self.sent.append(text)
        return {"delivered": True}


def _wire_channel(monkeypatch, adapter: _Adapter) -> None:
    monkeypatch.setattr(engine, "owner_conversation", lambda home, channel=None: ("telegram", "123"))
    monkeypatch.setattr(engine, "ADAPTERS", {"telegram": adapter})


def test_undeliverable_approval_parks_visibly(tmp_path: Path) -> None:
    """No channel configured: the run still parks (the CLI can approve it), but
    it parks marked, and the audit event is an error rather than silence."""
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _approval_dag("gated_undeliverable"))
    result = run_workflow(paths, "gated_undeliverable")
    gate = result["nodes"]["gate"]
    assert result["status"] == "awaiting_approval"
    assert gate["delivery"] == "undelivered"
    assert "no owner conversation" in gate["delivery_error"]
    assert gate["parked_at"]
    undeliverable = [e for e in _events(paths) if e["type"] == "workflow.approval_undeliverable"]
    assert len(undeliverable) == 1
    assert undeliverable[0]["status"] == "error"
    assert undeliverable[0]["details"]["code"] == gate["approval_code"]


def test_delivered_approval_records_delivery(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    adapter = _Adapter()
    _wire_channel(monkeypatch, adapter)
    _write_workflow(paths, _approval_dag("gated_delivered"))
    result = run_workflow(paths, "gated_delivered")
    assert result["nodes"]["gate"]["delivery"] == "delivered"
    assert "delivery_error" not in result["nodes"]["gate"]
    assert len(adapter.sent) == 1
    assert not [e for e in _events(paths) if e["type"] == "workflow.approval_undeliverable"]


def test_send_failure_marks_the_node_undelivered(tmp_path: Path, monkeypatch) -> None:
    """A channel that exists but rejects the send is just as undeliverable as
    one that was never configured — same state, so triage sees both."""
    paths = init_runtime(tmp_path, examples=True)
    _wire_channel(monkeypatch, _Adapter(fail=True))
    _write_workflow(paths, _approval_dag("gated_sendfail"))
    result = run_workflow(paths, "gated_sendfail")
    gate = result["nodes"]["gate"]
    assert gate["delivery"] == "undelivered"
    assert "send failed" in gate["delivery_error"]
    assert "bot token rejected" in gate["delivery_error"]
    assert [e for e in _events(paths) if e["type"] == "workflow.approval_undeliverable"]


def test_fresh_parked_approval_is_not_alarmed(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _approval_dag("gated_fresh"))
    run_workflow(paths, "gated_fresh")
    assert alarm_stale_approvals(paths) == []
    assert not [e for e in _events(paths) if e["type"] == "workflow.approval_stale"]


def test_stale_approval_alarms_exactly_once(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _approval_dag("gated_stale"))
    result = run_workflow(paths, "gated_stale")
    parked = datetime.fromisoformat(result["nodes"]["gate"]["parked_at"])
    later = parked + timedelta(hours=25)

    alarmed = alarm_stale_approvals(paths, now=later)
    assert [entry["node"] for entry in alarmed] == ["gate"]
    stale = [e for e in _events(paths) if e["type"] == "workflow.approval_stale"]
    assert len(stale) == 1
    assert stale[0]["status"] == "error"
    assert stale[0]["details"]["delivery"] == "undelivered"

    # Idempotent: the heartbeat runs this every tick, so it must not re-alarm.
    assert alarm_stale_approvals(paths, now=later + timedelta(hours=24)) == []
    assert len([e for e in _events(paths) if e["type"] == "workflow.approval_stale"]) == 1
    assert load_run(paths, result["id"])["nodes"]["gate"]["stale_alarm_at"]


def test_resolved_approval_is_never_alarmed(tmp_path: Path) -> None:
    """Advancement runs before the alarm sweep on the tick, so an approval that
    arrived is resolved before it can be reported as unanswered."""
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _approval_dag("gated_resolved"))
    result = run_workflow(paths, "gated_resolved")
    resolve(paths.approvals, result["nodes"]["gate"]["approval_code"], approved=True)
    advance_run(paths, load_run(paths, result["id"]))
    assert alarm_stale_approvals(paths, now=datetime.now(timezone.utc) + timedelta(days=40)) == []


def test_runs_listing_flags_undelivered_approvals(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _approval_dag("gated_listing"))
    run_workflow(paths, "gated_listing")
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "workflow", "runs"]) == 0
    out = capsys.readouterr().out
    assert "approval NOT DELIVERED" in out
    assert "jigga approve" in out


# --- plan + CLI -------------------------------------------------------------


def test_plan_reports_v2_nodes_and_runnability(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _approval_dag("gated_plan"))
    plan = plan_workflow(load_workflows(paths.workflows)["gated_plan"], load_agents(paths.agents))
    assert plan["engine"] == "v2"
    # needs_approval parks at runtime, so it does NOT block a v2 plan.
    assert plan["can_run"] is True
    gate = next(node for node in plan["nodes"] if node["id"] == "gate")
    assert gate["policy"]["status"] == "needs_approval"


def test_cli_runs_list_and_resume(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_workflow(paths, _approval_dag("gated_cli"))
    assert main(["--home", str(tmp_path), "workflow", "run", "gated_cli"]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "workflow", "runs", "--active"]) == 0
    out = capsys.readouterr().out
    assert "gated_cli" in out and "awaiting_approval" in out
    run_id = list_runs(paths, "gated_cli")[0]["id"]
    code = list_runs(paths, "gated_cli")[0]["nodes"]["gate"]["approval_code"]
    assert main(["--home", str(tmp_path), "approvals", "approve", code]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "workflow", "resume", run_id]) == 0
    assert '"status": "completed"' in capsys.readouterr().out

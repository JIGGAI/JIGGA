from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from types import SimpleNamespace

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.core.models import AgentConfig
from jigga.runtime.agent import _gate_tool_call, _to_tool_name, run_agent
from jigga.runtime.capabilities import CapabilityManifest
from jigga.runtime.approvals import (
    consume_if_approved,
    find_by_code,
    parse_approval_reply,
    pending_approvals,
    request_approval,
    resolve,
    resolve_and_requeue,
)
from jigga.runtime.channel_listener import ingest_once
from jigga.runtime.model_router import ModelCallResult, ModelToolCall
from jigga.runtime.tasks import create_task, list_tasks, set_task_state, task_requires_approval


def _result(content="", tool_calls=None) -> ModelCallResult:
    return ModelCallResult(status="ok", provider="dry_run", model="m", content=content, dry_run=True,
                           tool_calls=tool_calls or [])


def _scripted(seq):
    it = iter(seq)
    return lambda home, logs_dir, request: next(it)


def _write_agent(paths, aid, tools) -> None:
    write_yaml(paths.agents / f"{aid}.yaml", {"id": aid, "name": aid, "role": "tester",
               "memory_scope": "task_only", "tools": tools, "permissions": {}})


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


# --- module units ----------------------------------------------------------


def test_parse_approval_reply() -> None:
    assert parse_approval_reply("approve AB12CD") == (True, "AB12CD")
    assert parse_approval_reply("deny AB12CD") == (False, "AB12CD")
    assert parse_approval_reply("decline ab12cd") == (False, "ab12cd")
    assert parse_approval_reply("approve") is None       # no code
    assert parse_approval_reply("just a normal message") is None


def test_request_resolve_consume_once(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    rec = request_approval(paths.approvals, agent_id="a", task_id="t1", action="x.y", reason="risk")
    assert pending_approvals(paths.approvals)[0]["code"] == rec["code"]
    assert find_by_code(paths.approvals, rec["code"].lower()) is not None  # case-insensitive
    # not approved yet → gate must not let it through
    assert consume_if_approved(paths.approvals, task_id="t1", action="x.y") is False
    resolve(paths.approvals, rec["code"], approved=True)
    # approved → consumes exactly once
    assert consume_if_approved(paths.approvals, task_id="t1", action="x.y") is True
    assert consume_if_approved(paths.approvals, task_id="t1", action="x.y") is False


# --- agent loop: park on needs_approval, resume after approve --------------


def test_agent_parks_approval_then_resumes(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_agent(paths, "looper", ["spawn_subagent"])   # spawn_subagent is medium-risk
    task = create_task(paths.tasks, "do risky thing", assignee="looper")

    call1 = [_result(tool_calls=[ModelToolCall(id="c1", name=_to_tool_name("spawn_subagent"), arguments={})])]
    with patch("jigga.runtime.agent.call_model", _scripted(call1)):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "looper")

    pend = pending_approvals(paths.approvals)
    assert len(pend) == 1 and pend[0]["action"] == "spawn_subagent"
    assert list_tasks(paths.tasks)[0].state == "needs_approval"
    assert "approval.requested" in [e["type"] for e in _events(paths)]

    # Approve → task re-queued → re-run lets the action through (consumed).
    resolve_and_requeue(paths.approvals, paths.tasks, pend[0]["code"], approved=True)
    assert next(t for t in list_tasks(paths.tasks) if t.id == task.id).state == "pending"

    call2 = [_result(tool_calls=[ModelToolCall(id="c2", name=_to_tool_name("spawn_subagent"), arguments={})]),
             _result(content="done")]
    with patch("jigga.runtime.agent.call_model", _scripted(call2)):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "looper")
    types = [e["type"] for e in _events(paths)]
    assert "agent.tool_call.approved" in types
    assert "agent.tool_call.executed" in types


# --- channel reply resolves + re-queues ------------------------------------


def _enable_telegram(paths, *, allowed=("111",), default_agent="daily_briefing_agent") -> None:
    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": True, "allowed_chat_ids": list(allowed),
                          "default_agent": default_agent}}
    write_yaml(paths.config, config)


def test_channel_reply_resolves_and_requeues(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths)
    task = create_task(paths.tasks, "held", assignee="daily_briefing_agent")
    set_task_state(paths.tasks, task.id, "needs_approval")
    rec = request_approval(paths.approvals, agent_id="daily_briefing_agent", task_id=task.id,
                           action="spawn_subagent", reason="risk", channel="telegram", conversation_id=111)

    reply = {"status": "ok", "messages": [{"channel": "telegram", "chat_id": 111, "chat_type": "private",
             "sender": "a", "sender_id": 111, "text": f"approve {rec['code']}", "mentions_bot": False, "message_id": 1}]}
    with patch("jigga.runtime.telegram.poll_messages", return_value=reply), \
         patch("jigga.runtime.telegram.send_message", return_value={"status": "ok"}), \
         patch("jigga.runtime.channel_listener.run_agent") as run_mock:
        summary = ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0)

    assert summary["created"] == []                                  # reply is not a new task
    assert pending_approvals(paths.approvals) == []                  # resolved
    assert next(t for t in list_tasks(paths.tasks) if t.id == task.id).state == "pending"  # re-queued
    assert "approval.resolved" in [e["type"] for e in _events(paths)]
    run_mock.assert_called()                                         # held task's agent re-run


# --- CLI -------------------------------------------------------------------


def test_cli_approvals_list_and_approve(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    task = create_task(paths.tasks, "x", assignee="a")
    rec = request_approval(paths.approvals, agent_id="a", task_id=task.id, action="spawn_subagent")

    assert main(["--home", str(tmp_path), "approvals", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["code"] == rec["code"]

    assert main(["--home", str(tmp_path), "approvals", "approve", rec["code"]]) == 0
    assert pending_approvals(paths.approvals) == []
    assert list_tasks(paths.tasks)[0].state == "pending"

# --- ticket-level approval opt-in (auto-run by default) --------------------
#
# Policy: work moves through the pipeline on its own. A ticket only parks for a
# human when its writer asks for that in the ticket itself. Before this, a
# medium-risk action like `task.assign` parked every ticket under the default
# `ask` mode, and the parked approval was filed with no channel — so nothing was
# ever sent and the board silently stopped.


def _write_agent_mode(paths, aid, tools, mode) -> None:
    write_yaml(paths.agents / f"{aid}.yaml", {"id": aid, "name": aid, "role": "tester",
               "memory_scope": "task_only", "tools": tools, "permissions": {},
               "permission_mode": mode})


def test_task_requires_approval_reads_metadata_flag() -> None:
    task = SimpleNamespace(description="anything", metadata={"requires_approval": True})
    assert task_requires_approval(task) is True


def test_task_requires_approval_reads_marker_case_insensitively() -> None:
    for body in ("Approval: required",
                 "## Context\nblah\napproval: REQUIRED\n## Requirements\nx",
                 "Approval:required"):
        task = SimpleNamespace(description=body, metadata={})
        assert task_requires_approval(task) is True, body


def test_task_requires_approval_false_for_ordinary_ticket() -> None:
    body = "## Requirements\nCreate a simple nextjs project that says hello world"
    assert task_requires_approval(SimpleNamespace(description=body, metadata={})) is False
    assert task_requires_approval(SimpleNamespace(description=None, metadata=None)) is False


def test_task_requires_approval_ignores_a_mere_mention() -> None:
    # "approval" appearing in prose must not park the ticket — only the directive does.
    body = "Ask the client for approval before launch. Approval: not required"
    assert task_requires_approval(SimpleNamespace(description=body, metadata={})) is False


def _cap(risk="medium", permissions=None):
    return CapabilityManifest.from_dict(
        {"name": "c", "version": "0.1.0", "summary": "s", "actions": ["a"],
         "risk_level": risk, "handler": "h", "permissions": permissions or {}},
        source="test", bundled=False)


def test_gate_autonomous_allows_medium_risk_without_marker() -> None:
    agent = AgentConfig(id="a", name="a", role="r", tools=["a"], permissions={})
    assert _gate_tool_call(_cap(), agent, "autonomous", force_approval=False) == ("allow", None)


def test_gate_ticket_marker_parks_even_in_autonomous() -> None:
    agent = AgentConfig(id="a", name="a", role="r", tools=["a"], permissions={})
    verdict, reason = _gate_tool_call(_cap(), agent, "autonomous", force_approval=True)
    assert verdict == "needs_approval"
    assert "ticket" in (reason or "")


def test_gate_marker_parks_low_risk_too() -> None:
    # The ticket writer's request is about the work, not the capability's risk.
    agent = AgentConfig(id="a", name="a", role="r", tools=["a"], permissions={})
    verdict, _ = _gate_tool_call(_cap(risk="low"), agent, "autonomous", force_approval=True)
    assert verdict == "needs_approval"


def test_gate_explicit_deny_still_wins_over_autonomous() -> None:
    agent = AgentConfig(id="a", name="a", role="r", tools=["a"],
                        permissions={"network": {"mode": "deny"}})
    cap = _cap(permissions={"network": {"mode": "allow", "target": "example.com"}})
    verdict, _ = _gate_tool_call(cap, agent, "autonomous", force_approval=False)
    assert verdict == "deny"


def test_autonomous_ticket_runs_straight_through(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_agent_mode(paths, "lead", ["spawn_subagent"], "autonomous")
    create_task(paths.tasks, "ship it", description="## Requirements\nbuild the thing",
                assignee="lead")

    calls = [_result(tool_calls=[ModelToolCall(id="c1", name=_to_tool_name("spawn_subagent"), arguments={})]),
             _result(content="done")]
    with patch("jigga.runtime.agent.call_model", _scripted(calls)):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "lead")

    assert pending_approvals(paths.approvals) == []
    assert list_tasks(paths.tasks)[0].state != "needs_approval"
    assert "agent.tool_call.executed" in [e["type"] for e in _events(paths)]


def test_ticket_marker_parks_and_notifies_default_channel(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths, allowed=("6477250615",))
    config = read_yaml(paths.config)
    config.setdefault("channels", {})["default"] = "telegram"
    write_yaml(paths.config, config)

    _write_agent_mode(paths, "lead", ["spawn_subagent"], "autonomous")
    create_task(paths.tasks, "risky", description="Approval: required\n\n## Requirements\nx",
                assignee="lead")

    calls = [_result(tool_calls=[ModelToolCall(id="c1", name=_to_tool_name("spawn_subagent"), arguments={})])]
    with patch("jigga.runtime.agent.call_model", _scripted(calls)), \
         patch("jigga.runtime.telegram.send_message", return_value={"status": "ok"}) as send:
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "lead")

    pend = pending_approvals(paths.approvals)
    assert len(pend) == 1
    assert list_tasks(paths.tasks)[0].state == "needs_approval"
    # The whole point: a ticket with no channel of its own still reaches a human.
    assert pend[0]["channel"] == "telegram"
    send.assert_called_once()
    sent = " ".join(str(a) for a in send.call_args.args) + str(send.call_args.kwargs)
    assert pend[0]["code"] in sent           # the human is told how to approve
    assert "risky" in sent                   # ...and which ticket it is


def test_parked_approval_without_any_channel_is_logged_not_silent(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)   # no channels configured
    _write_agent_mode(paths, "lead", ["spawn_subagent"], "autonomous")
    create_task(paths.tasks, "risky", description="Approval: required", assignee="lead")

    calls = [_result(tool_calls=[ModelToolCall(id="c1", name=_to_tool_name("spawn_subagent"), arguments={})])]
    with patch("jigga.runtime.agent.call_model", _scripted(calls)):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "lead")

    assert len(pending_approvals(paths.approvals)) == 1
    types = [e["type"] for e in _events(paths)]
    assert "approval.unnotified" in types   # loud about the case we cannot deliver


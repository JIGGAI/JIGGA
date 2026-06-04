"""File-backed agent mailbox (W6 / #62): send → surface on wake → mark read
after a successful run. File-first: every message is a JSON file in the
recipient's workspace inbox, never moved or deleted."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.mailbox import (
    inbox_dir,
    list_messages,
    mark_read,
    render_unread,
    send_message,
    unread_messages,
)
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.tasks import create_task


def _agent(paths, agent_id="helper", **extra):
    write_yaml(paths.agents / f"{agent_id}.yaml", {
        "id": agent_id, "name": agent_id.title(), "role": "helps",
        "memory_scope": "task_only", "tools": [], "permissions": {}, **extra})


# --- module ------------------------------------------------------------------


def test_send_list_unread_mark_roundtrip(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    first = send_message(paths.home, "helper", "helper", "check the launch copy",
                         sender="chief", subject="launch")
    second = send_message(paths.home, "helper", "helper", "also: standup moved to 10",
                          sender="chief")

    messages = list_messages(paths.home, "helper", "helper")
    assert [m["id"] for m in messages] == [first["id"], second["id"]]   # oldest first
    assert len(unread_messages(paths.home, "helper", "helper")) == 2

    assert mark_read(paths.home, "helper", "helper", [first["id"]]) == 1
    assert [m["id"] for m in unread_messages(paths.home, "helper", "helper")] == [second["id"]]
    # idempotent + file never deleted (auditable correspondence record)
    assert mark_read(paths.home, "helper", "helper", [first["id"]]) == 0
    assert (inbox_dir(paths.home, "helper", "helper") / f"{first['id']}.json").exists()


def test_send_validates_and_bounds_body(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    with pytest.raises(ValueError):
        send_message(paths.home, "helper", "", "body", sender="x")
    with pytest.raises(ValueError):
        send_message(paths.home, "helper", "helper", "  ", sender="x")
    message = send_message(paths.home, "helper", "helper", "x" * 10_000, sender="x")
    assert len(message["body"]) == 4000                                  # flood guard


def test_render_unread_bounds_output() -> None:
    messages = [{"id": f"msg_{i}", "from": "chief", "body": f"item {i}",
                 "created_at": "2026-06-04T10:00:00"} for i in range(8)]
    text = render_unread(messages)
    assert "8 unread" in text and "item 0" in text and "item 4" in text
    assert "item 5" not in text and "and 3 more" in text                 # capped at 5
    assert render_unread([]) == ""


# --- delivery loop: surface on wake → mark read after a successful run --------


def _ok_model(home, logs_dir, request):
    return ModelCallResult(status="ok", provider="dry_run", model="m", content="done",
                           dry_run=True, tool_calls=[])


def test_unread_surfaces_in_context_and_marked_read_after_success(tmp_path: Path) -> None:
    from jigga.runtime.agent import run_agent

    paths = init_runtime(tmp_path)
    _agent(paths)
    send_message(paths.home, "helper", "helper", "please review the aurora draft", sender="chief")
    create_task(paths.tasks, "any work", assignee="helper")
    captured: dict = {}

    def spy(home, logs_dir, request):
        captured["system"] = request.items[0].content
        return _ok_model(home, logs_dir, request)

    with patch("jigga.runtime.agent.call_model", spy):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "helper")

    assert "aurora draft" in captured["system"]                          # surfaced on wake
    assert unread_messages(paths.home, "helper", "helper") == []         # marked read post-success
    events = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "mailbox.read" for e in events)


def test_failed_run_does_not_mark_read(tmp_path: Path) -> None:
    from jigga.runtime.agent import run_agent

    paths = init_runtime(tmp_path)
    _agent(paths)
    send_message(paths.home, "helper", "helper", "important: rotate the token", sender="chief")
    create_task(paths.tasks, "doomed work", assignee="helper")

    def boom(home, logs_dir, request):  # model error → task state "failed"
        return ModelCallResult(status="error", provider="dry_run", model="m", content="",
                               dry_run=True, tool_calls=[], error="model down")

    with patch("jigga.runtime.agent.call_model", boom):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "helper")

    # the failed run must re-see the message next wake
    assert len(unread_messages(paths.home, "helper", "helper")) == 1


def test_inbox_is_private_layer_omitted_when_restricted(tmp_path: Path) -> None:
    from jigga.runtime.agent import run_agent

    paths = init_runtime(tmp_path)
    _agent(paths)
    send_message(paths.home, "helper", "helper", "secret: the zebra plan", sender="chief")
    create_task(paths.tasks, "public reply", assignee="helper",
                metadata={"restricted_memory": True})
    captured: dict = {}

    def spy(home, logs_dir, request):
        captured["system"] = request.items[0].content
        return _ok_model(home, logs_dir, request)

    with patch("jigga.runtime.agent.call_model", spy):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "helper")

    assert "zebra plan" not in captured["system"]          # private layer withheld in group session


# --- mailbox.send capability ---------------------------------------------------


def test_mailbox_send_capability_delivers_to_recipients_workspace(tmp_path: Path) -> None:
    """An agent sending to a teammate in ANOTHER team must land in the
    recipient's home workspace — where the recipient actually wakes."""
    from jigga.core.models import WorkflowStep
    from jigga.runtime.handlers import _mailbox_handler
    from jigga.runtime.runtime_context import RuntimeContext
    from jigga.core.models import AgentConfig

    paths = init_runtime(tmp_path)
    write_yaml(paths.teams / "mt.yaml", {"id": "mt", "name": "Marketing",
               "agents": [{"id": "writer", "role": "drafting"}],
               "routing": {"default_assignee": "writer"}})
    sender = AgentConfig(id="chief", name="Chief", role="runs things")
    runtime = RuntimeContext(agent=sender, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    out = _mailbox_handler(WorkflowStep(id="s", action="mailbox.send"), None,
                           {"to": "writer", "body": "brief: solstice launch", "subject": "brief"},
                           {}, runtime)
    assert out["workspace"] == "mt"                        # recipient's team, not the sender's
    assert unread_messages(paths.home, "mt", "writer")[0]["from"] == "chief"
    events = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "mailbox.sent" for e in events)


def test_mailbox_is_a_bundled_capability(tmp_path: Path) -> None:
    from jigga.runtime.capabilities import CapabilityRegistry

    paths = init_runtime(tmp_path)
    registry = CapabilityRegistry.load(user_capabilities=paths.capabilities,
                                       approvals_dir=paths.policies)
    cap = registry.resolve_action("mailbox.send")
    assert cap is not None and cap.name == "mailbox"


# --- CLI (human → agent) --------------------------------------------------------


def test_cli_send_and_list(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "mailbox", "send", "assistant",
                 "--body", "remember to water the plants", "--subject", "chores", "--json"]) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["from"] == "human" and sent["to"] == "assistant"

    assert main(["--home", str(tmp_path), "mailbox", "list", "assistant", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1 and listed[0]["id"] == sent["id"]

    assert main(["--home", str(tmp_path), "mailbox", "list", "assistant"]) == 0
    out = capsys.readouterr().out
    assert "water the plants" in out and out.lstrip().startswith("*")    # unread marker


def test_messages_are_searchable(tmp_path: Path) -> None:
    from jigga.runtime.memory_index import search_memory

    paths = init_runtime(tmp_path)
    send_message(paths.home, "helper", "helper", "the quarterly zebra budget is approved",
                 sender="chief")
    results = search_memory(paths.memory, "quarterly zebra budget", rebuild=True)
    assert results and results[0]["layer"] == "role:helper"


# --- mail wake: unread mail wakes an idle recipient within a tick --------------


def test_supervisor_wakes_idle_agent_with_unread_mail(tmp_path: Path) -> None:
    from jigga.runtime.supervisor import supervisor_tick
    from jigga.runtime.tasks import list_tasks

    paths = init_runtime(tmp_path)
    _agent(paths)
    send_message(paths.home, "helper", "helper", "ping: status on the aurora copy?", sender="chief")

    with patch("jigga.runtime.agent.call_model", _ok_model):
        supervisor_tick(paths.home)

    mail_tasks = [t for t in list_tasks(paths.tasks) if (t.metadata or {}).get("mail_wake")]
    assert len(mail_tasks) == 1 and mail_tasks[0].state == "completed"
    assert unread_messages(paths.home, "helper", "helper") == []        # delivered + marked read
    events = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "supervisor.mail_wake" for e in events)


def test_mail_wake_does_not_stack_tasks_while_throttled(tmp_path: Path) -> None:
    """A throttled wake leaves the mail task pending — the next tick must NOT
    queue another one (no task pileup from one message)."""
    from jigga.core.io import read_yaml, write_yaml
    from jigga.runtime.loop_guard import load_loop_state, now_utc, record_wake, save_loop_state
    from jigga.runtime.supervisor import supervisor_tick
    from jigga.runtime.tasks import list_tasks

    paths = init_runtime(tmp_path)
    _agent(paths)
    config = read_yaml(paths.config)
    config["supervisor"] = {"max_wakes_per_agent_per_hour": 1}
    write_yaml(paths.config, config)
    state = load_loop_state(paths.home)
    record_wake(state, "helper", now_utc())               # exhaust the throttle
    save_loop_state(paths.home, state)
    send_message(paths.home, "helper", "helper", "you there?", sender="chief")

    with patch("jigga.runtime.agent.call_model", _ok_model):
        supervisor_tick(paths.home)
        supervisor_tick(paths.home)                        # second tick: task pending, throttled

    mail_tasks = [t for t in list_tasks(paths.tasks) if (t.metadata or {}).get("mail_wake")]
    assert len(mail_tasks) == 1                            # no pileup
    assert mail_tasks[0].state == "pending"                # still waiting for the throttle window


def test_agent_with_pending_work_gets_no_extra_mail_task(tmp_path: Path) -> None:
    """An agent that's waking anyway delivers its inbox through that run — no
    synthetic mail task needed."""
    from jigga.runtime.supervisor import supervisor_tick
    from jigga.runtime.tasks import list_tasks

    paths = init_runtime(tmp_path)
    _agent(paths)
    create_task(paths.tasks, "real work", assignee="helper")
    send_message(paths.home, "helper", "helper", "fyi: deadline moved", sender="chief")

    with patch("jigga.runtime.agent.call_model", _ok_model):
        supervisor_tick(paths.home)

    assert not any((t.metadata or {}).get("mail_wake") for t in list_tasks(paths.tasks))
    assert unread_messages(paths.home, "helper", "helper") == []        # still delivered


def test_no_unread_mail_no_wake(tmp_path: Path) -> None:
    from jigga.runtime.supervisor import supervisor_tick
    from jigga.runtime.tasks import list_tasks

    paths = init_runtime(tmp_path)
    _agent(paths)
    supervisor_tick(paths.home)
    assert not any((t.metadata or {}).get("mail_wake") for t in list_tasks(paths.tasks))


def test_mail_wake_not_duplicated_over_crash_leftover_task(tmp_path: Path) -> None:
    """A claimed/running mail task (e.g. a crashed run's leftover) is invisible
    to pending_summary — the queued-mail-wakes guard must still prevent a
    duplicate synthetic task."""
    from jigga.runtime.supervisor import supervisor_tick
    from jigga.runtime.tasks import list_tasks, set_task_state

    paths = init_runtime(tmp_path)
    _agent(paths)
    send_message(paths.home, "helper", "helper", "you there?", sender="chief")
    leftover = create_task(paths.tasks, "Unread mailbox messages", assignee="helper",
                           metadata={"mail_wake": True})
    set_task_state(paths.tasks, leftover.id, "claimed")    # crash mid-run leftover

    with patch("jigga.runtime.agent.call_model", _ok_model):
        supervisor_tick(paths.home)

    mail_tasks = [t for t in list_tasks(paths.tasks) if (t.metadata or {}).get("mail_wake")]
    assert len(mail_tasks) == 1                             # the leftover only — no duplicate

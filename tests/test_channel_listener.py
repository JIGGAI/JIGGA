from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.channel_listener import channel_listen, enabled_channels, ingest_once
from jigga.runtime.tasks import list_tasks


def _enable_telegram(paths, *, default_agent="daily_briefing_agent", allowed=("111",)) -> None:
    config = read_yaml(paths.config)
    config["channels"] = {
        "telegram": {
            "enabled": True,
            "allowed_chat_ids": list(allowed),
            "default_agent": default_agent,
        }
    }
    write_yaml(paths.config, config)


def _msg(text="hi", chat_id=111, sender="alice", message_id=10) -> dict:
    return {
        "channel": "telegram",
        "chat_id": chat_id,
        "sender": sender,
        "sender_id": chat_id,
        "text": text,
        "message_id": message_id,
    }


def _events(paths):
    path = paths.logs / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- enabled_channels ------------------------------------------------------


def test_enabled_channels_empty_by_default(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    assert enabled_channels(paths.home) == []


def test_enabled_channels_lists_enabled_telegram(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _enable_telegram(paths)
    channels = enabled_channels(paths.home)
    assert [name for name, _ in channels] == ["telegram"]


def test_enabled_channels_skips_disabled(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": False, "default_agent": "x"}}
    write_yaml(paths.config, config)
    assert enabled_channels(paths.home) == []


def test_enabled_channels_skips_unknown_channel(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    config = read_yaml(paths.config)
    config["channels"] = {"smoke_signals": {"enabled": True, "default_agent": "x"}}
    write_yaml(paths.config, config)
    assert enabled_channels(paths.home) == []  # no registered poller


# --- ingest_once -----------------------------------------------------------


def test_ingest_creates_task_per_message_and_runs_agent(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths)
    poll_result = {"status": "ok", "messages": [_msg(text="hello there", chat_id=111)]}
    ran = []

    def fake_run_agent(home, logs, tasks, agents, agent_id, **kw):
        ran.append(agent_id)
        return {"agent_id": agent_id}

    with patch("jigga.runtime.telegram.poll_messages", return_value=poll_result), patch(
        "jigga.runtime.channel_listener.run_agent", fake_run_agent
    ):
        summary = ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0)

    assert len(summary["created"]) == 1
    task = list_tasks(paths.tasks)[0]
    assert task.assignee == "daily_briefing_agent"
    assert task.metadata["chat_id"] == 111
    assert task.metadata["channel"] == "telegram"
    assert "hello there" in task.description
    assert "chat_id=111" in task.description  # reply hint
    assert ran == ["daily_briefing_agent"]
    assert "channel.message.received" in [e["type"] for e in _events(paths)]


def test_ingest_no_messages_is_noop(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths)
    with patch("jigga.runtime.telegram.poll_messages", return_value={"status": "ok", "messages": []}), patch(
        "jigga.runtime.channel_listener.run_agent"
    ) as run_mock:
        summary = ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0)
    assert summary["created"] == []
    run_mock.assert_not_called()
    assert list_tasks(paths.tasks) == []


def test_ingest_skips_not_connected_channel(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths)
    with patch(
        "jigga.runtime.telegram.poll_messages",
        return_value={"status": "telegram.not_connected", "messages": []},
    ):
        summary = ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0)
    assert summary["created"] == []
    assert "channel.poll_skipped" in [e["type"] for e in _events(paths)]


def test_ingest_no_process_skips_agents(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths)
    with patch("jigga.runtime.telegram.poll_messages", return_value={"status": "ok", "messages": [_msg()]}), patch(
        "jigga.runtime.channel_listener.run_agent"
    ) as run_mock:
        summary = ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0, process_agents=False)
    assert len(summary["created"]) == 1
    run_mock.assert_not_called()


def test_failed_channel_task_replies_with_error(tmp_path: Path, monkeypatch) -> None:
    """When the agent run fails (e.g. model rate-limited), the user gets a short
    error reply in the chat instead of silence."""
    from jigga.runtime.model_router import ModelCallResult

    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths)
    sent: list[tuple] = []
    monkeypatch.setattr("jigga.runtime.telegram.send_message",
                        lambda home, chat_id, text: sent.append((chat_id, text)) or {"sent": True})
    err = ModelCallResult(status="error", provider="chatgpt", model="m", content="", dry_run=False, tool_calls=[])
    with patch("jigga.runtime.telegram.poll_messages",
               return_value={"status": "ok", "messages": [_msg(text="hi", chat_id=111)]}), \
         patch("jigga.runtime.agent.call_model", return_value=err):
        ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0, process_agents=True)

    task = list_tasks(paths.tasks)[0]
    assert task.state == "failed"                       # the run failed
    assert sent and sent[0][0] == 111                   # …and we replied to that chat
    assert "try again" in sent[0][1].lower()
    assert "channel.failure_notified" in [e["type"] for e in _events(paths)]


def test_successful_channel_task_gets_no_error_reply(tmp_path: Path, monkeypatch) -> None:
    """A successful run must NOT trigger the failure reply."""
    from jigga.runtime.model_router import ModelCallResult

    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths)
    sent: list[tuple] = []
    monkeypatch.setattr("jigga.runtime.telegram.send_message",
                        lambda home, chat_id, text: sent.append((chat_id, text)) or {"sent": True})
    ok = ModelCallResult(status="ok", provider="x", model="m", content="done", dry_run=False, tool_calls=[])
    with patch("jigga.runtime.telegram.poll_messages",
               return_value={"status": "ok", "messages": [_msg(text="hi", chat_id=111)]}), \
         patch("jigga.runtime.agent.call_model", return_value=ok):
        ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0, process_agents=True)

    assert sent == []  # no error reply on success


# --- channel_listen bounded loop -------------------------------------------


def test_channel_listen_bounded(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths)
    with patch("jigga.runtime.telegram.poll_messages", return_value={"status": "ok", "messages": [_msg()]}), patch(
        "jigga.runtime.channel_listener.run_agent", return_value={}
    ):
        result = channel_listen(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0, max_cycles=2)
    assert result["status"] == "stopped"
    assert result["cycles"] == 2
    # 2 cycles × 1 message each
    assert len(list_tasks(paths.tasks)) == 2


def test_channel_listen_emits_lifecycle_events(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths)
    with patch("jigga.runtime.telegram.poll_messages", return_value={"status": "ok", "messages": []}), patch(
        "jigga.runtime.channel_listener.run_agent", return_value={}
    ):
        channel_listen(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0, max_cycles=1)
    types = [e["type"] for e in _events(paths)]
    assert "channel.listen.started" in types
    assert "channel.listen.stopped" in types


# --- graceful shutdown via subprocess --------------------------------------


def test_channel_listen_handles_sigterm(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths)
    # Drive via subprocess (signal handlers need the main thread). The listener
    # runs unbounded with a fast empty poll; SIGTERM should stop it cleanly.
    code = (
        "import json, sys\n"
        "from unittest.mock import patch\n"
        "from jigga.core.paths import get_paths\n"
        "from jigga.runtime import channel_listener\n"
        f"paths = get_paths({str(tmp_path)!r})\n"
        "with patch('jigga.runtime.telegram.poll_messages', return_value={'status':'ok','messages':[]}):\n"
        "    r = channel_listener.channel_listen(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0, max_cycles=None)\n"
        "sys.stdout.write(json.dumps({'status': r['status'], 'sig': r['stopped_by_signal']}))\n"
        "sys.stdout.flush()\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.6)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode == 0, f"stderr={stderr}"
    payload = json.loads(stdout)
    assert payload["status"] == "interrupted"
    assert payload["sig"] == int(signal.SIGTERM)


# --- H0 regression: `channels listen` CLI routing -------------------------


def test_cli_channels_listen_reaches_listener(tmp_path: Path) -> None:
    """Regression guard: `jigga channels listen` must actually invoke
    channel_listen. It previously fell through to a no-op `return 0` because the
    handler was orphaned after a `return` in the approvals block."""
    from jigga.cli import main

    init_runtime(tmp_path)
    with patch("jigga.cli.channel_listen", return_value={
        "status": "ok", "cycles": 1, "stopped_by_signal": None,
    }) as fake:
        rc = main(["--home", str(tmp_path), "channels", "listen",
                   "--max-cycles", "1", "--no-process"])
    assert rc == 0
    assert fake.called, "channels listen did not reach channel_listen"


def test_ingest_rejects_message_from_unauthorized_sender(tmp_path: Path) -> None:
    """The inbound auth boundary: a message from a chat_id NOT in the allowlist
    must be dropped — no task created — and logged as rejected. Proves the
    listener actually enforces identity_allowed, not just that the predicate works."""
    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths, allowed=("111",))
    poll_result = {"status": "ok", "messages": [_msg(text="let me in", chat_id=999)]}

    with patch("jigga.runtime.telegram.poll_messages", return_value=poll_result), patch(
        "jigga.runtime.channel_listener.run_agent"
    ) as run_mock:
        summary = ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0)

    assert summary["created"] == []                       # no task for an unauthorized sender
    assert list_tasks(paths.tasks) == []
    run_mock.assert_not_called()
    assert "channel.message.rejected" in [e["type"] for e in _events(paths)]


def test_ingest_falls_back_to_default_agent_when_channel_has_none(tmp_path: Path) -> None:
    """When a channel sets no default_agent, inbound routes to the global default
    (chief) agent — the catch-all."""
    from jigga.core.io import write_yaml as _wy
    paths = init_runtime(tmp_path)
    _enable_telegram(paths, default_agent=None)
    # a default/chief agent exists
    _wy(paths.agents / "chief.yaml", {"id": "chief", "name": "Chief", "role": "chief",
        "default": True, "memory_scope": "task_only", "tools": [], "permissions": {}})
    with patch("jigga.runtime.telegram.poll_messages",
               return_value={"status": "ok", "messages": [_msg(text="hi", chat_id=111)]}), \
         patch("jigga.runtime.channel_listener.run_agent", return_value={}):
        ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0)
    assert list_tasks(paths.tasks)[0].assignee == "chief"

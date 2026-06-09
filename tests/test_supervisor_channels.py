from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.supervisor import supervisor_tick
from jigga.runtime.tasks import list_tasks


def _enable_telegram(paths, *, default_agent="daily_briefing_agent", allowed=("111",)) -> None:
    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": True, "allowed_chat_ids": list(allowed),
                                       "default_agent": default_agent}}
    write_yaml(paths.config, config)


def _msg(text="hi", chat_id=111) -> dict:
    return {"channel": "telegram", "chat_id": chat_id, "sender": "alice", "sender_id": chat_id,
            "text": text, "message_id": 10}


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _no_tool_result(home, logs_dir, request) -> ModelCallResult:
    return ModelCallResult(status="ok", provider="dry_run", model="m", content="ok", dry_run=True, tool_calls=[])


def test_supervisor_polls_channel_and_runs_agent(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths)
    poll = {"status": "ok", "messages": [_msg(text="summarize my day", chat_id=111)]}
    with patch("jigga.runtime.telegram.poll_messages", return_value=poll), \
         patch("jigga.runtime.agent.call_model", _no_tool_result):
        result = supervisor_tick(paths.home)

    # The channel message became a task...
    tasks = list_tasks(paths.tasks)
    chan_task = next(t for t in tasks if t.metadata.get("channel") == "telegram")
    assert "summarize my day" in (chan_task.description or "")
    # ...and the agent ran it immediately during ingest (user-initiated, so it
    # bypasses the tick's wake-throttle) — completed, not left pending.
    assert chan_task.state == "completed"
    assert result is not None
    types = [e["type"] for e in _events(paths)]
    assert "channel.message.received" in types
    assert "agent.run.started" in types


def test_channel_chat_is_not_wake_throttled(tmp_path: Path) -> None:
    """A user chatting must not be rate-limited by the autonomous-loop throttle.
    With the agent already at its wake limit (as if a cron wake just fired), a
    channel message must STILL run + complete — because channel ingest runs the
    agent directly, bypassing the tick's wake-throttle."""
    from jigga.runtime.loop_guard import load_loop_state, now_utc, record_wake, save_loop_state

    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths, default_agent="daily_briefing_agent")
    config = read_yaml(paths.config)
    config["supervisor"] = {"max_wakes_per_agent_per_hour": 1}
    write_yaml(paths.config, config)
    # Exhaust the throttle for the agent so the tick's agent loop WOULD skip it.
    state = load_loop_state(paths.home)
    record_wake(state, "daily_briefing_agent", now_utc())
    save_loop_state(paths.home, state)

    poll = {"status": "ok", "messages": [_msg(text="still answer me", chat_id=111)]}
    with patch("jigga.runtime.telegram.poll_messages", return_value=poll), \
         patch("jigga.runtime.agent.call_model", _no_tool_result):
        supervisor_tick(paths.home)

    chan_task = next(t for t in list_tasks(paths.tasks) if t.metadata.get("channel") == "telegram")
    assert chan_task.state == "completed"  # ran despite the agent being at its wake limit


def test_supervisor_no_channels_is_noop(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)  # channels disabled by default
    with patch("jigga.runtime.telegram.poll_messages") as poll_mock:
        supervisor_tick(paths.home)
    poll_mock.assert_not_called()
    assert "channel.message.received" not in [e["type"] for e in _events(paths)]


def test_supervisor_contains_channel_errors(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable_telegram(paths)
    with patch("jigga.runtime.telegram.poll_messages", side_effect=RuntimeError("network down")):
        result = supervisor_tick(paths.home)  # must NOT raise
    assert isinstance(result, dict)
    errors = [e for e in _events(paths) if e["type"] == "channel.ingest_error"]
    assert errors and "network down" in errors[-1]["details"]["error"]


def test_backoff_class_exponential_and_reset() -> None:
    from jigga.runtime.supervisor import _Backoff

    b = _Backoff(base=5.0, cap=300.0)
    assert not b.should_skip(0.0)
    assert b.record_failure(0.0) == 5.0
    assert b.should_skip(4.9) and not b.should_skip(5.0)
    assert b.record_failure(5.0) == 10.0
    assert b.record_failure(15.0) == 20.0
    b.fails = 10
    assert b.record_failure(0.0) == 300.0  # capped
    b.record_success()
    assert b.fails == 0 and not b.should_skip(0.0)


def test_channel_poll_backs_off_on_repeated_errors(tmp_path: Path, monkeypatch) -> None:
    """A sustained channel fault (e.g. Telegram 409) must not retry/log every
    tick — it backs off, skipping polls within the cooldown window."""
    from jigga.runtime import supervisor
    from jigga.runtime.supervisor import _channel_backoff, _poll_channels

    paths = init_runtime(tmp_path)
    _enable_telegram(paths, default_agent="x")
    _channel_backoff.record_success()  # reset shared process state

    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise RuntimeError("HTTP 409: terminated by other getUpdates request")

    monkeypatch.setattr(supervisor, "ingest_once", boom)
    clock = {"t": 0.0}

    _poll_channels(paths, clock=lambda: clock["t"])  # 1st: fails, 5s cooldown
    assert calls["n"] == 1
    clock["t"] = 3.0
    _poll_channels(paths, clock=lambda: clock["t"])  # within cooldown → skipped
    assert calls["n"] == 1
    clock["t"] = 6.0
    _poll_channels(paths, clock=lambda: clock["t"])  # cooldown elapsed → retries
    assert calls["n"] == 2

    errs = [e for e in _events(paths) if e["type"] == "channel.ingest_error"]
    assert len(errs) == 2  # one per actual attempt, not per tick
    assert errs[-1]["details"]["consecutive"] == 2
    assert errs[-1]["details"]["retry_in_seconds"] >= 10

    _channel_backoff.record_success()  # leave shared state clean for other tests

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
    assert any(t.metadata.get("channel") == "telegram" and "summarize my day" in (t.description or "") for t in tasks)
    # ...and the tick's own agent loop ran the assignee (one execution path).
    assert "daily_briefing_agent" in result["targets"]
    types = [e["type"] for e in _events(paths)]
    assert "channel.message.received" in types
    assert "agent.run.started" in types


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

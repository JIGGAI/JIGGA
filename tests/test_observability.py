from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.runtime.audit import REDACTED, append_event, redact
from jigga.runtime.audit_query import (
    format_event,
    parse_since,
    query_events,
    tail_events,
    trace,
)


# --- redaction -------------------------------------------------------------


def test_redact_drops_sensitive_keys() -> None:
    out = redact({"api_key": "anything", "note": "fine"}, key="details")
    assert out["api_key"] == REDACTED
    assert out["note"] == "fine"


def test_redact_scrubs_value_patterns_in_strings() -> None:
    assert redact("key is sk-ABCDEFGH012345678901") == f"key is {REDACTED}"
    assert REDACTED in redact("token 123456789:AAEabcdefghijklmnopqrstuvwxyz012345")
    assert REDACTED in redact("Authorization: Bearer abcdef0123456789ABCDEF")


def test_redact_recurses_into_nested_structures() -> None:
    out = redact({"outer": {"password": "hunter2", "items": ["ghp_" + "a" * 30]}}, key="root")
    assert out["outer"]["password"] == REDACTED
    assert out["outer"]["items"][0] == REDACTED


def test_append_event_redacts_before_writing(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    append_event(logs, "model.call.failed", status="error", bot_token="123456789:AAEsecretsecretsecretsecretsecret00", note="ok")
    line = (logs / "events.jsonl").read_text(encoding="utf-8").strip()
    assert "secretsecret" not in line
    assert REDACTED in line
    event = json.loads(line)
    assert event["details"]["bot_token"] == REDACTED  # key-based
    assert event["details"]["note"] == "ok"


def test_redaction_leaves_ordinary_text_alone() -> None:
    # Don't over-redact: normal prose with words/numbers must survive.
    text = "Processed 42 emails and 3 events for user alice in 1.2s"
    assert redact(text) == text


# --- parse_since -----------------------------------------------------------


def test_parse_since_relative_durations() -> None:
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    assert parse_since("30m", now=now) == now - timedelta(minutes=30)
    assert parse_since("24h", now=now) == now - timedelta(hours=24)
    assert parse_since("7d", now=now) == now - timedelta(days=7)
    assert parse_since("2w", now=now) == now - timedelta(weeks=2)


def test_parse_since_iso_and_invalid() -> None:
    parsed = parse_since("2026-05-29T00:00:00Z")
    assert parsed.year == 2026
    with pytest.raises(ValueError):
        parse_since("yesterday")


# --- query / tail / trace --------------------------------------------------


def _seed(logs: Path) -> None:
    append_event(logs, "agent.run.started", agent="alpha", run_id="run_1")
    append_event(logs, "agent.tool_call.executed", agent="alpha", run_id="run_1", action="x.y")
    append_event(logs, "agent.run.completed", agent="alpha", run_id="run_1")
    append_event(logs, "agent.run.started", agent="beta", run_id="run_2")
    append_event(logs, "policy.denied", status="deny", agent="beta", run_id="run_2", reason="nope")


def test_query_filters_by_agent(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    _seed(logs)
    alpha = query_events(logs, agent="alpha")
    assert {e["details"]["agent"] for e in alpha} == {"alpha"}
    assert len(alpha) == 3


def test_query_filters_by_type_family(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    _seed(logs)
    runs = query_events(logs, type_filter="agent.run")
    assert {e["type"] for e in runs} == {"agent.run.started", "agent.run.completed"}


def test_query_filters_by_status(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    _seed(logs)
    denied = query_events(logs, status="deny")
    assert len(denied) == 1
    assert denied[0]["type"] == "policy.denied"


def test_query_limit_keeps_most_recent(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    _seed(logs)
    last_two = query_events(logs, limit=2)
    assert [e["type"] for e in last_two] == ["agent.run.started", "policy.denied"]


def test_query_since_excludes_old_events(tmp_path: Path, monkeypatch) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    # Write an event, then query with a future-ish cutoff to exclude it.
    append_event(logs, "agent.run.started", agent="alpha")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert query_events(logs, since=future) == []
    # And a generous window includes it.
    assert len(query_events(logs, since="1h")) == 1


def test_tail_returns_last_n(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    _seed(logs)
    assert len(tail_events(logs, 2)) == 2
    assert tail_events(logs, 2)[-1]["type"] == "policy.denied"


def test_trace_correlates_by_run_id(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    _seed(logs)
    chain = trace(logs, "run_1")
    assert len(chain) == 3
    assert all(e["details"]["run_id"] == "run_1" for e in chain)


def test_trace_matches_event_id_and_prefix(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    logs = tmp_path / "logs"
    event = append_event(logs, "agent.run.started", agent="alpha")
    assert trace(logs, event["id"]) == [event]
    # prefix match
    assert trace(logs, event["id"][:10])[0]["id"] == event["id"]


def test_format_event_is_one_line() -> None:
    event = {"time": "2026-05-29T12:00:00+00:00", "type": "agent.run.started",
             "status": "ok", "details": {"agent": "alpha", "run_id": "r1"}}
    line = format_event(event)
    assert "\n" not in line
    assert "agent.run.started" in line
    assert "agent=alpha" in line


def test_format_event_flags_non_ok_status() -> None:
    event = {"time": "t", "type": "policy.denied", "status": "deny", "details": {"reason": "x"}}
    assert "[deny]" in format_event(event)


# --- CLI -------------------------------------------------------------------


def test_cli_logs_tail(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    _seed(tmp_path / "logs")
    assert main(["--home", str(tmp_path), "logs", "tail", "-n", "2"]) == 0
    out = capsys.readouterr().out
    assert "agent.run.started" in out or "policy.denied" in out


def test_cli_audit_json(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    _seed(tmp_path / "logs")
    assert main(["--home", str(tmp_path), "audit", "--agent", "alpha", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert all(e["details"]["agent"] == "alpha" for e in payload)


def test_cli_trace(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    _seed(tmp_path / "logs")
    assert main(["--home", str(tmp_path), "trace", "run_2", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {e["details"]["run_id"] for e in payload} == {"run_2"}

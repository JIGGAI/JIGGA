"""Telemetry: default-off, payload contains counts only (no content), report
== send payload, daily guard, supervisor containment with unreachable endpoint."""

from __future__ import annotations

import json
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime import telemetry
from jigga.runtime.audit import append_event
from jigga.runtime.supervisor import supervisor_tick


def test_default_off_and_toggle(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    assert telemetry.enabled(paths.home) is False
    telemetry.set_enabled(paths.home, True)
    assert telemetry.enabled(paths.home) is True
    assert telemetry.maybe_send.__doc__  # sanity


def test_payload_counts_only_never_content(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    append_event(paths.logs, "agent.run.started", agent="chief",
                 description="SENSITIVE user message text")
    append_event(paths.logs, "model.call", status="error", error="boom SECRET")
    payload = telemetry.build_payload(paths.home)
    dumped = json.dumps(payload)
    assert "SENSITIVE" not in dumped and "SECRET" not in dumped and "chief" not in dumped
    assert payload["event_counts"]["agent.run.started"] == 1
    assert payload["error_counts"]["model.call"] == 1
    assert payload["shape"]["agents"] >= 1 and payload["schema"] == 1
    assert payload["install_id"].startswith("install_")
    # install id is stable
    assert telemetry.build_payload(paths.home)["install_id"] == payload["install_id"]


def test_maybe_send_respects_optout_and_daily_guard(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    sent = []
    monkeypatch.setattr(telemetry, "send", lambda home: sent.append(1) or {"sent": True, "endpoint": "x", "status": 200})
    assert telemetry.maybe_send(paths.home) is None  # default off
    telemetry.set_enabled(paths.home, True)
    from jigga.core.io import write_json
    telemetry.install_id(paths.home)
    assert telemetry.maybe_send(paths.home) is not None and len(sent) == 1
    state = telemetry._state(paths.home)
    state["last_sent_at"] = "2099-01-01T00:00:00+00:00"
    write_json(telemetry._state_path(paths.home), state)
    # (future last_sent → within window → skip)
    assert telemetry.maybe_send(paths.home) is None and len(sent) == 1


def test_supervisor_contains_unreachable_endpoint(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.config, {"telemetry": {"enabled": True,
                                            "endpoint": "http://127.0.0.1:9/ingest"}})
    result = supervisor_tick(paths.home)  # must not raise
    assert "runs" in result
    events = (paths.logs / "events.jsonl").read_text(encoding="utf-8")
    assert "telemetry.send_error" in events

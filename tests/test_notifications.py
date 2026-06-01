from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.notifications import (
    NotificationRequest,
    NotificationResult,
    _escape_applescript,
    _send_linux,
    _send_macos,
    delivery_mode,
    send_notification,
)
from jigga.runtime.workflow import run_workflow


# --- delivery_mode resolution ----------------------------------------------


def test_delivery_mode_defaults_to_real_when_no_override_or_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("JIGGA_NOTIFICATION_MODE", raising=False)
    # No config.yaml at all → default "real".
    assert delivery_mode(tmp_path) == "real"


def test_delivery_mode_env_override_wins_over_config(tmp_path: Path, monkeypatch) -> None:
    init_runtime(tmp_path, examples=True)
    # Config writes "real" on init; env override flips it.
    monkeypatch.setenv("JIGGA_NOTIFICATION_MODE", "dry_run")
    assert delivery_mode(tmp_path) == "dry_run"


def test_delivery_mode_reads_config_when_no_env_override(tmp_path: Path, monkeypatch) -> None:
    init_runtime(tmp_path, examples=True)
    monkeypatch.delenv("JIGGA_NOTIFICATION_MODE", raising=False)
    write_yaml(
        tmp_path / "config.yaml",
        {
            "version": 1,
            "home": str(tmp_path),
            "notifications": {"delivery_mode": "dry_run"},
        },
    )
    assert delivery_mode(tmp_path) == "dry_run"


# --- send_notification dry-run path ----------------------------------------


def test_dry_run_does_not_spawn_subprocess(monkeypatch) -> None:
    sentinel = MagicMock(side_effect=AssertionError("subprocess.run must not be called in dry_run"))
    monkeypatch.setattr(subprocess, "run", sentinel)
    result = send_notification(NotificationRequest("hi", "there"), dry_run=True)
    assert result.delivered is False
    assert result.backend == "dry_run"
    assert sentinel.call_count == 0


# --- Linux notify-send path ------------------------------------------------


def test_linux_dispatch_uses_notify_send(monkeypatch) -> None:
    monkeypatch.setattr("jigga.runtime.notifications.shutil.which", lambda _: "/usr/bin/notify-send")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        result = subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return result

    monkeypatch.setattr("jigga.runtime.notifications.subprocess.run", fake_run)
    result = _send_linux(NotificationRequest("Title", "Body text", urgency="high"))
    assert result.delivered is True
    assert result.backend == "notify-send"
    assert captured["cmd"][0] == "notify-send"
    assert "--app-name=JIGGA" in captured["cmd"]
    assert "--urgency=critical" in captured["cmd"]  # high → critical
    assert "Title" in captured["cmd"]
    assert "Body text" in captured["cmd"]


def test_linux_missing_notify_send_fails_gracefully(monkeypatch) -> None:
    monkeypatch.setattr("jigga.runtime.notifications.shutil.which", lambda _: None)
    result = _send_linux(NotificationRequest("Title", "Body"))
    assert result.delivered is False
    assert result.backend == "linux"
    assert "notify-send not installed" in (result.error or "")


def test_linux_nonzero_exit_captures_stderr(monkeypatch) -> None:
    monkeypatch.setattr("jigga.runtime.notifications.shutil.which", lambda _: "/usr/bin/notify-send")
    monkeypatch.setattr(
        "jigga.runtime.notifications.subprocess.run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="bus disconnected"),
    )
    result = _send_linux(NotificationRequest("t", "b"))
    assert result.delivered is False
    assert result.backend == "notify-send"
    assert "bus disconnected" in (result.error or "")


# --- macOS osascript path --------------------------------------------------


def test_macos_dispatch_invokes_osascript(monkeypatch) -> None:
    monkeypatch.setattr("jigga.runtime.notifications.shutil.which", lambda _: "/usr/bin/osascript")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("jigga.runtime.notifications.subprocess.run", fake_run)
    result = _send_macos(NotificationRequest("My Title", "Hello"))
    assert result.delivered is True
    assert result.backend == "osascript"
    assert captured["cmd"][:2] == ["osascript", "-e"]
    assert 'display notification "Hello"' in captured["cmd"][2]
    assert 'with title "My Title"' in captured["cmd"][2]


# --- AppleScript escaping --------------------------------------------------


def test_applescript_escape_handles_quotes_and_backslashes() -> None:
    # Order matters: backslashes first, then quotes.
    assert _escape_applescript('plain') == "plain"
    assert _escape_applescript('with "quotes"') == 'with \\"quotes\\"'
    assert _escape_applescript("path\\to\\file") == "path\\\\to\\\\file"
    assert _escape_applescript('line1\nline2') == "line1 line2"
    # The classic injection attempt becomes harmless text inside the AppleScript
    # string: every embedded quote is escaped so the AppleScript parser sees a
    # single literal string, not a closing quote followed by a `do shell script` call.
    injection = '"; do shell script "rm -rf ~"; --'
    escaped = _escape_applescript(injection)
    assert escaped.count('\\"') == injection.count('"')
    assert "rm -rf" in escaped  # text survives, just as data


# --- Top-level send_notification platform dispatch -------------------------


def test_send_notification_routes_by_platform(monkeypatch) -> None:
    monkeypatch.setattr("jigga.runtime.notifications.platform.system", lambda: "Linux")
    monkeypatch.setattr(
        "jigga.runtime.notifications._send_linux",
        lambda req: NotificationResult(delivered=True, backend="notify-send"),
    )
    assert send_notification(NotificationRequest("a", "b")).backend == "notify-send"

    monkeypatch.setattr("jigga.runtime.notifications.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "jigga.runtime.notifications._send_macos",
        lambda req: NotificationResult(delivered=True, backend="osascript"),
    )
    assert send_notification(NotificationRequest("a", "b")).backend == "osascript"

    monkeypatch.setattr("jigga.runtime.notifications.platform.system", lambda: "Windows")
    result = send_notification(NotificationRequest("a", "b"))
    assert result.delivered is False
    assert result.backend.startswith("unsupported-")


# --- End-to-end via workflow dispatch --------------------------------------


def test_workflow_notify_step_emits_audit_and_real_handler_output(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    result = run_workflow(
        paths, "morning_day_summary"
    )
    assert result["status"] == "completed"

    notify_output = result["outputs"]["notify"]
    # New shape: came through the real handler, not the legacy dry_run stub.
    assert notify_output["source"] == "capability.notifications"
    assert notify_output["dry_run"] is True  # conftest forces test-mode dry_run
    assert notify_output["delivered"] is False
    assert notify_output["title"] == "JIGGA"
    # summarize_day produces {"summary": "...", ...} which the body coercer
    # extracts as a string.
    assert isinstance(notify_output["body"], str)
    assert notify_output["body"]

    events = [
        json.loads(line)
        for line in (paths.logs / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    notification_events = [e for e in events if e["type"].startswith("notification.")]
    assert notification_events
    # Failed because dry-run never delivers; explicit field tells the user why.
    assert notification_events[-1]["type"] == "notification.failed"
    assert notification_events[-1]["details"]["dry_run"] is True


def test_workflow_notify_step_uses_explicit_title_when_supplied(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(
        paths.workflows / "explicit_notify.yaml",
        {
            "id": "explicit_notify",
            "name": "Explicit Notify",
            "steps": [
                {
                    "id": "notify",
                    "agent": "daily_briefing_agent",
                    "action": "notifications.send",
                    "input": {
                        "title": "Meeting reminder",
                        "body": "Standup at 9:30",
                        "urgency": "high",
                    },
                }
            ],
        },
    )
    result = run_workflow(
        paths, "explicit_notify"
    )
    assert result["status"] == "completed"
    notify = result["outputs"]["notify"]
    assert notify["title"] == "Meeting reminder"
    assert notify["body"] == "Standup at 9:30"
    assert notify["urgency"] == "high"


def test_dry_run_capability_handler_remains_available() -> None:
    # Tests/workflows can still pin a capability to dry_run.notifications if
    # they explicitly want the legacy stub behaviour.
    from jigga.runtime.dispatcher import HANDLERS

    assert "dry_run.notifications" in HANDLERS
    assert "runtime.notifications" in HANDLERS

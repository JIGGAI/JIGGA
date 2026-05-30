from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.audit import append_event
from jigga.runtime.audit_query import read_events
from jigga.runtime.log_rotation import archive_files, rotate_logs


def _set_rotation(paths, **rotation) -> None:
    config = read_yaml(paths.config)
    config["logs"] = {"rotation": rotation}
    write_yaml(paths.config, config)


def _write_line(logs: Path, time_iso: str, *, event_type: str = "seed", **details) -> None:
    """Write one event with a controlled timestamp (append_event uses now())."""
    event = {"id": "evt_x", "time": time_iso, "type": event_type, "status": "ok", "details": details}
    with (logs / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


# --- rollover triggers -----------------------------------------------------


def test_rollover_by_day(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    logs = paths.logs
    _write_line(logs, "2026-05-29T10:00:00+00:00", n=1)
    # "Now" is the next day → the active log rolls into a dated archive.
    result = rotate_logs(paths.home, logs, now=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc))
    assert result["rotated"] == "events-2026-05-29.jsonl"
    assert not (logs / "events.jsonl").exists()
    assert (logs / "events-2026-05-29.jsonl").exists()


def test_no_rollover_same_day(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _write_line(paths.logs, "2026-05-30T08:00:00+00:00", n=1)
    result = rotate_logs(paths.home, paths.logs, now=datetime(2026, 5, 30, 23, 0, tzinfo=timezone.utc))
    assert result["rotated"] is None
    assert (paths.logs / "events.jsonl").exists()


def test_rollover_by_size(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_rotation(paths, enabled=True, max_bytes=200, retention_days=30)
    logs = paths.logs
    now = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
    for i in range(20):
        _write_line(logs, "2026-05-30T12:00:00+00:00", filler="x" * 40, i=i)
    result = rotate_logs(paths.home, logs, now=now)
    assert result["rotated"] == "events-2026-05-30.jsonl"  # same-day size split
    assert not (logs / "events.jsonl").exists()


def test_disabled_rotation_is_a_noop(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_rotation(paths, enabled=False)
    _write_line(paths.logs, "2020-01-01T00:00:00+00:00", n=1)
    result = rotate_logs(paths.home, paths.logs, now=datetime(2026, 5, 30, tzinfo=timezone.utc))
    assert result["rotated"] is None
    assert (paths.logs / "events.jsonl").exists()


# --- retention -------------------------------------------------------------


def test_prune_drops_archives_past_retention(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_rotation(paths, enabled=True, retention_days=7)
    logs = paths.logs
    (logs / "events-2026-05-01.jsonl").write_text('{"old": 1}\n', encoding="utf-8")  # 29 days old
    (logs / "events-2026-05-29.jsonl").write_text('{"recent": 1}\n', encoding="utf-8")  # 1 day old
    result = rotate_logs(paths.home, logs, now=datetime(2026, 5, 30, tzinfo=timezone.utc))
    assert "events-2026-05-01.jsonl" in result["pruned"]
    assert not (logs / "events-2026-05-01.jsonl").exists()
    assert (logs / "events-2026-05-29.jsonl").exists()


# --- readers fold archives back in -----------------------------------------


def test_read_events_includes_archives_chronologically(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    logs = paths.logs
    (logs / "events-2026-05-28.jsonl").write_text(
        json.dumps({"id": "a", "time": "2026-05-28T00:00:00+00:00", "type": "old", "status": "ok", "details": {}}) + "\n",
        encoding="utf-8",
    )
    append_event(logs, "fresh")  # goes to the active log
    events = read_events(logs)
    assert [e["type"] for e in events] == ["old", "fresh"]


def test_budget_window_survives_a_rollover(tmp_path: Path) -> None:
    # Spend recorded yesterday (now archived) must still count toward the cap.
    from jigga.runtime.cost import agent_spend

    paths = init_runtime(tmp_path)
    logs = paths.logs
    _write_line(logs, "2026-05-29T10:00:00+00:00", event_type="model.call", agent_id="alpha", cost_usd=3.0)
    rotate_logs(paths.home, logs, now=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc))
    assert not (logs / "events.jsonl").exists()  # yesterday's spend is now archived
    _write_line(logs, "2026-05-30T10:00:00+00:00", event_type="model.call", agent_id="alpha", cost_usd=1.0)
    # Spend reads archive + active → both days count.
    assert agent_spend(logs, "alpha", since=None) == 4.0


# --- CLI -------------------------------------------------------------------


def test_cli_logs_rotate(tmp_path: Path, capsys) -> None:
    from datetime import timedelta

    paths = init_runtime(tmp_path)
    # Yesterday → triggers a day rollover but stays within the 30-day retention.
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    _write_line(paths.logs, yesterday.isoformat(), n=1)
    assert main(["--home", str(tmp_path), "logs", "rotate", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rotated"] == f"events-{yesterday.date().isoformat()}.jsonl"
    assert archive_files(paths.logs)

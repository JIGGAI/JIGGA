from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import write_json
from jigga.runtime.scheduler import _friendly_schedule_due, _parse_friendly_time
from jigga.runtime.team import run_team


def test_friendly_time_parser_supports_common_forms() -> None:
    assert _parse_friendly_time("weekday 7:30am") == (7, 30)
    assert _parse_friendly_time("weekdays at 07:30") == (7, 30)
    assert _parse_friendly_time("daily 9:00") == (9, 0)
    assert _parse_friendly_time("weekend 10am") == (10, 0)
    assert _parse_friendly_time("every day at 6:30pm") == (18, 30)
    assert _parse_friendly_time("12am sharp") == (0, 0)
    assert _parse_friendly_time("12pm sharp") == (12, 0)
    assert _parse_friendly_time("no time here") is None


def test_friendly_schedule_due_respects_day_of_week() -> None:
    # 2026-05-25 is a Monday (weekday=0)
    monday_730 = datetime(2026, 5, 25, 7, 30)
    saturday_730 = datetime(2026, 5, 30, 7, 30)
    assert _friendly_schedule_due("weekday 7:30am", monday_730) is True
    assert _friendly_schedule_due("weekday 7:30am", saturday_730) is False
    assert _friendly_schedule_due("daily 7:30am", saturday_730) is True
    assert _friendly_schedule_due("weekend 10am", saturday_730) is False  # 10am, not 7:30
    assert _friendly_schedule_due("weekend 10am", datetime(2026, 5, 30, 10, 0)) is True


def test_atomic_write_json_replaces_via_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    write_json(target, {"a": 1})
    # No leftover .tmp sibling after a successful write
    assert not (tmp_path / "state.json.tmp").exists()
    # Subsequent writes also leave no tmp file behind
    write_json(target, {"a": 2, "b": 3})
    assert not (tmp_path / "state.json.tmp").exists()
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2, "b": 3}


def test_supervisor_loop_handles_sigterm_cleanly(tmp_path: Path) -> None:
    init_runtime(tmp_path, examples=True)
    # signal.signal() only works on the main thread, so we drive the test via a
    # subprocess (matches `jigga supervisor start` real usage) and send SIGTERM
    # to it. The child reports the final loop result as JSON on stdout.
    code = (
        "import json, sys\n"
        "from jigga.runtime.daemon import supervisor_loop\n"
        f"result = supervisor_loop({str(tmp_path)!r}, interval_seconds=0.1, max_ticks=None)\n"
        "sys.stdout.write(json.dumps({"
        "'status': result['status'], "
        "'stopped_by_signal': result['stopped_by_signal'], "
        "'tick_count': result['tick_count']"
        "}))\n"
        "sys.stdout.flush()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Give the subprocess a moment to install handlers and tick at least once.
    time.sleep(0.6)
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode == 0, f"subprocess failed: stderr={stderr}"
    result = json.loads(stdout)
    assert result["status"] == "interrupted"
    assert result["stopped_by_signal"] == int(signal.SIGTERM)
    assert result["tick_count"] >= 1


def test_team_runtime_surfaces_handoffs_in_audit_and_metadata(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    result = run_team(
        paths.home,
        paths.logs,
        paths.tasks,
        paths.teams,
        paths.workflows,
        paths.agents,
        paths.memory,
        "social_content_team",
    )
    assert result["handoffs"], "social_content_team has handoffs declared in YAML"
    # Coordination task metadata carries the handoffs forward
    assert result["created_tasks"][0]["metadata"]["handoffs"] == result["handoffs"]
    # Audit log contains the declaration event
    events = [
        json.loads(line)
        for line in (paths.logs / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    declared = [event for event in events if event["type"] == "team.handoffs_declared"]
    assert declared
    assert declared[-1]["details"]["team"] == "social_content_team"

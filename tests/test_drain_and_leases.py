"""Assertion 9 — a restart drains rather than orphaning a claim.

(prior-gen stack) `launchctl kickstart -k` hard-killed a workflow worker
mid-task. That orphaned its queue claim and stalled the run until the 120s
lease expired, with nothing on the record explaining the pause.

> Any restart path that can hit a worker holding a lease should drain, not
> kill. And leases need a visible expiry so a stalled run is diagnosable
> rather than mysterious.

JIGGA already drained *in the loop*: SIGTERM sets a flag and the tick finishes
before exit. The hole was underneath it — the init system never waited. The
systemd unit carried no `TimeoutStopSec`, so the 90s default applied while a
tick may legitimately run for `supervisor.max_tick_seconds` (300s by default,
raised in #189). systemd would escalate to SIGKILL mid-agent and orphan the
claim anyway; launchd's 20s default was tighter still. The drain logic was
correct and simply never got the time to run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime import doctor
from jigga.runtime.recovery import held_leases
from jigga.runtime.service import (
    APP_STOP_TIMEOUT_SECONDS,
    render_app_launchd,
    render_app_systemd,
    render_launchd_plist,
    render_systemd_unit,
    service_argv,
    stop_timeout_seconds,
)
from jigga.runtime.tasks import create_task, set_task_state


def _argv() -> list[str]:
    return service_argv("/usr/bin/python3", 60)


# --- the init system has to wait for the drain ------------------------------


def test_the_stop_timeout_exceeds_the_tick_budget(tmp_path: Path) -> None:
    """The bug: a 300s tick budget under a 90s stop timeout guarantees a
    SIGKILL mid-agent. The timeout must leave room for the tick to finish."""
    from jigga.core.config import max_tick_seconds

    init_runtime(tmp_path)
    assert stop_timeout_seconds(tmp_path) > max_tick_seconds(tmp_path)


def test_raising_the_tick_budget_raises_the_stop_timeout(tmp_path: Path) -> None:
    """Derived, not fixed — otherwise raising the budget silently reintroduces
    the hard kill."""
    init_runtime(tmp_path)
    before = stop_timeout_seconds(tmp_path)
    config = tmp_path / "config.yaml"
    data = read_yaml(config)
    data["supervisor"] = {"max_tick_seconds": 1800}
    write_yaml(config, data)
    assert stop_timeout_seconds(tmp_path) > before
    assert stop_timeout_seconds(tmp_path) > 1800


def test_unbounded_ticks_still_get_a_finite_stop_timeout(tmp_path: Path) -> None:
    """`max_tick_seconds: 0` means unbounded ticks, but waiting forever would
    wedge `systemctl restart` — the ceiling is deliberate."""
    init_runtime(tmp_path)
    config = tmp_path / "config.yaml"
    data = read_yaml(config)
    data["supervisor"] = {"max_tick_seconds": 0}
    write_yaml(config, data)
    timeout = stop_timeout_seconds(tmp_path)
    assert 0 < timeout < 100000


def test_the_systemd_unit_carries_the_stop_timeout(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    unit = render_systemd_unit(_argv(), tmp_path)
    assert f"TimeoutStopSec={stop_timeout_seconds(tmp_path)}" in unit


def test_the_launchd_plist_carries_the_exit_timeout(tmp_path: Path) -> None:
    """launchd's default ExitTimeOut is 20s — tighter than systemd's, and the
    Mac is where iMessage runs."""
    paths = init_runtime(tmp_path)
    plist = render_launchd_plist(_argv(), tmp_path, paths.logs)
    assert "<key>ExitTimeOut</key>" in plist
    assert f"<integer>{stop_timeout_seconds(tmp_path)}</integer>" in plist


def test_plugin_apps_also_get_a_graceful_stop_window(tmp_path: Path) -> None:
    """Apps hold no task leases, but they are long-running servers — a hard
    kill can cut an in-flight request or an open SQLite write."""
    paths = init_runtime(tmp_path)
    unit = render_app_systemd("kitchen", ["/bin/true"], cwd=tmp_path, env={})
    plist = render_app_launchd("kitchen", ["/bin/true"], cwd=tmp_path, env={}, logs_dir=paths.logs)
    assert f"TimeoutStopSec={APP_STOP_TIMEOUT_SECONDS}" in unit
    assert "<key>ExitTimeOut</key>" in plist
    assert f"<integer>{APP_STOP_TIMEOUT_SECONDS}</integer>" in plist


def test_apps_get_a_shorter_window_than_the_supervisor(tmp_path: Path) -> None:
    """They wait on no model call, so making them wait as long as the
    supervisor would just make every restart slow."""
    init_runtime(tmp_path)
    assert APP_STOP_TIMEOUT_SECONDS < stop_timeout_seconds(tmp_path)


# --- the drain leaves a record ----------------------------------------------


def test_a_signalled_stop_is_recorded_at_both_ends(tmp_path: Path) -> None:
    """Without this a killed drain is indistinguishable from a crash. The
    `draining` event is written from the signal handler precisely so it
    survives a SIGKILL escalation that prevents `drained` from being written.
    """
    import json
    import signal

    from jigga.runtime.daemon import supervisor_loop

    paths = init_runtime(tmp_path)

    # Deliver the signal to ourselves during the first tick, then let the loop
    # finish that tick and exit.
    import os
    import threading

    def _signal_soon() -> None:
        import time as _t
        _t.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_signal_soon, daemon=True).start()
    result = supervisor_loop(tmp_path, interval_seconds=0.01, max_ticks=50)

    assert result["stopped_by_signal"] == signal.SIGTERM
    assert result["drain_seconds"] is not None
    rows = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines() if line.strip()]
    types = [r["type"] for r in rows]
    assert "supervisor.draining" in types
    assert "supervisor.drained" in types


def test_a_clean_stop_records_no_drain(tmp_path: Path) -> None:
    """`max_ticks` exhaustion is not a drain — the fields must not imply one."""
    from jigga.runtime.daemon import supervisor_loop

    init_runtime(tmp_path)
    result = supervisor_loop(tmp_path, interval_seconds=0.01, max_ticks=1)
    assert result["stopped_by_signal"] is None
    assert result["drain_seconds"] is None


# --- leases have a visible expiry -------------------------------------------


def test_a_held_claim_reports_when_it_expires(tmp_path: Path) -> None:
    """The expiry used to be implicit — `updated_at` plus a threshold buried in
    config — which is what made a stall mysterious rather than merely stuck."""
    paths = init_runtime(tmp_path)
    task = create_task(paths.tasks, title="long job", assignee="alpha")
    set_task_state(paths.tasks, task.id, "running")

    rows = held_leases(paths)

    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "task" and row["id"] == task.id and row["holder"] == "alpha"
    assert row["expires_at"] and not row["expired"]
    assert row["seconds_remaining"] > 0


def test_a_pending_task_holds_no_claim(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    create_task(paths.tasks, title="not started", assignee="alpha")
    assert held_leases(paths) == []


def test_an_expired_claim_is_flagged(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    task = create_task(paths.tasks, title="orphaned", assignee="alpha")
    set_task_state(paths.tasks, task.id, "running")

    later = datetime.now(timezone.utc) + timedelta(hours=48)
    rows = held_leases(paths, now=later)

    assert rows[0]["expired"] is True
    assert rows[0]["seconds_remaining"] < 0


def test_doctor_reads_an_expired_claim_as_a_dead_supervisor(tmp_path: Path) -> None:
    """The diagnosis, not the symptom: the sweep runs every tick, so an expired
    claim still sitting there means the supervisor isn't ticking."""
    paths = init_runtime(tmp_path)
    task = create_task(paths.tasks, title="orphaned", assignee="alpha")
    set_task_state(paths.tasks, task.id, "running")
    # Backdate the claim past the stale threshold.
    from jigga.core.io import read_json, write_json
    record = read_json(paths.tasks / f"{task.id}.json")
    record["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    write_json(paths.tasks / f"{task.id}.json", record)

    check = doctor._check_leases(paths)

    assert check.status == doctor.FAIL
    assert task.id in check.detail
    assert "supervisor isn't ticking" in (check.hint or "")


def test_doctor_is_quiet_when_nothing_is_claimed(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    assert doctor._check_leases(paths).status == doctor.OK


def test_doctor_shows_the_next_expiry_while_work_is_in_flight(tmp_path: Path) -> None:
    """A held claim is normal. It should read as informative, not alarming."""
    paths = init_runtime(tmp_path)
    task = create_task(paths.tasks, title="working", assignee="alpha")
    set_task_state(paths.tasks, task.id, "running")

    check = doctor._check_leases(paths)

    assert check.status == doctor.OK
    assert "next expires in" in check.detail

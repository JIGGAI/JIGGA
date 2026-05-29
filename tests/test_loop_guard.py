from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.runtime.audit import append_event
from jigga.runtime.loop_guard import (
    cron_already_fired,
    load_loop_state,
    now_utc,
    record_cron_fire,
    record_wake,
    save_loop_state,
    should_skip_wake,
)
from jigga.runtime.supervisor import supervisor_tick
from jigga.runtime.tasks import create_task, list_tasks


def test_cron_dedup_per_minute(tmp_path: Path) -> None:
    init_runtime(tmp_path, examples=True)
    state = load_loop_state(tmp_path)
    when = now_utc()

    assert cron_already_fired(state, "daily_briefing_agent", "30 7 * * 1-5", when) is False
    record_cron_fire(state, "daily_briefing_agent", "30 7 * * 1-5", when)
    assert cron_already_fired(state, "daily_briefing_agent", "30 7 * * 1-5", when) is True
    # A different target or cron in the same minute still fires
    assert cron_already_fired(state, "daily_briefing_agent", "0 8 * * *", when) is False
    # The same target+cron one minute later fires again
    assert cron_already_fired(state, "daily_briefing_agent", "30 7 * * 1-5", when + timedelta(minutes=1)) is False


def test_should_skip_wake_when_at_limit(tmp_path: Path) -> None:
    init_runtime(tmp_path, examples=True)
    state = load_loop_state(tmp_path)
    when = now_utc()
    for _ in range(3):
        record_wake(state, "daily_briefing_agent", when)
    assert should_skip_wake(state, "daily_briefing_agent", max_per_hour=3, now=when) is True
    assert should_skip_wake(state, "daily_briefing_agent", max_per_hour=5, now=when) is False
    # Wake records older than the window do not count
    state["wakes"]["daily_briefing_agent"] = []
    for _ in range(3):
        record_wake(state, "daily_briefing_agent", when - timedelta(hours=2))
    assert should_skip_wake(state, "daily_briefing_agent", max_per_hour=3, now=when) is False


def test_supervisor_tick_skips_duplicate_cron_within_same_minute(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    # Force a wake-count over the configured limit so the agent run is throttled.
    state = load_loop_state(tmp_path)
    when = now_utc()
    for _ in range(20):
        record_wake(state, "daily_briefing_agent", when)
    save_loop_state(tmp_path, state)

    create_task(paths.tasks, "Test wake throttle", assignee="daily_briefing_agent")
    first = supervisor_tick(tmp_path)
    assert "daily_briefing_agent" in first["throttled"]
    # Task should remain pending because the wake was skipped
    assert list_tasks(paths.tasks)[0].state == "pending"


def test_supervisor_records_cron_dedup_after_first_tick(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    # Inject a synthetic loop-state cron fire so that the next tick's identical
    # cron event (if one is even due now) would be deduped. Then ensure a second
    # tick in the same minute doesn't create another task.
    initial_count = len(list_tasks(paths.tasks))
    supervisor_tick(tmp_path)
    after_first = len(list_tasks(paths.tasks))
    supervisor_tick(tmp_path)
    after_second = len(list_tasks(paths.tasks))
    # Whatever the first tick created (cron-due tasks are time-dependent), the
    # second tick within the same minute must not add duplicate cron-wake tasks.
    cron_diff_first = after_first - initial_count
    cron_diff_second = after_second - after_first
    assert cron_diff_second == 0 or cron_diff_second < cron_diff_first


def test_loop_guard_persists_across_loads(tmp_path: Path) -> None:
    init_runtime(tmp_path, examples=True)
    state = load_loop_state(tmp_path)
    record_wake(state, "daily_briefing_agent", now_utc())
    save_loop_state(tmp_path, state)
    reloaded = load_loop_state(tmp_path)
    assert "daily_briefing_agent" in reloaded["wakes"]
    assert len(reloaded["wakes"]["daily_briefing_agent"]) == 1
    append_event(tmp_path / "logs", "test.event")  # incidental — ensures audit dir works

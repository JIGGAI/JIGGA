"""The supervisor loop runs channels in near-real-time when one is enabled:
each tick long-polls (returns the instant a message arrives) and the loop runs
ticks back-to-back with no inter-tick sleep. With no channel it keeps the
classic one-tick-then-sleep(interval) cadence.
"""

from __future__ import annotations

from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime import daemon


def _enable_telegram(tmp_path: Path) -> None:
    cfg = read_yaml(tmp_path / "config.yaml")
    cfg["channels"] = {"telegram": {"enabled": True, "allowed_chat_ids": ["1"], "default_agent": "x"}}
    write_yaml(tmp_path / "config.yaml", cfg)


def _record(monkeypatch):
    polls: list[int] = []
    sleeps: list[float] = []
    monkeypatch.setattr(
        "jigga.runtime.daemon.supervisor_tick",
        lambda home, *, channel_long_poll_seconds=0: polls.append(channel_long_poll_seconds) or {"status": "ok"},
    )
    monkeypatch.setattr("jigga.runtime.daemon.time.sleep", lambda s: sleeps.append(s))
    return polls, sleeps


def test_loop_long_polls_and_skips_sleep_when_channel_enabled(tmp_path: Path, monkeypatch) -> None:
    init_runtime(tmp_path)
    _enable_telegram(tmp_path)
    polls, sleeps = _record(monkeypatch)

    daemon.supervisor_loop(str(tmp_path), interval_seconds=60, max_ticks=2, channel_long_poll_seconds=7)

    assert polls == [7, 7]   # every tick long-polls the channel
    assert sleeps == [0]     # no 60s inter-tick gap — replies stay near-real-time


def test_loop_keeps_cron_cadence_with_no_channel(tmp_path: Path, monkeypatch) -> None:
    init_runtime(tmp_path)  # no channel enabled
    polls, sleeps = _record(monkeypatch)

    daemon.supervisor_loop(str(tmp_path), interval_seconds=60, max_ticks=2, channel_long_poll_seconds=7)

    assert polls == [0, 0]   # single non-blocking poll per tick
    assert sleeps == [60]    # classic one-tick-then-sleep(interval)

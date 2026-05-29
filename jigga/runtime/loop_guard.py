from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jigga.core.io import read_json, write_json

WAKE_WINDOW = timedelta(hours=1)


def _state_path(home: Path) -> Path:
    return home / "loop_state.json"


def load_loop_state(home: Path) -> dict[str, Any]:
    path = _state_path(home)
    if not path.exists():
        return {"wakes": {}, "cron_fired": {}}
    data = read_json(path)
    data.setdefault("wakes", {})
    data.setdefault("cron_fired", {})
    return data


def save_loop_state(home: Path, state: dict[str, Any]) -> None:
    write_json(_state_path(home), state)


def _bucket(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M")


def cron_already_fired(state: dict[str, Any], target: str, cron: str, when: datetime) -> bool:
    return state["cron_fired"].get(f"{target}|{cron}") == _bucket(when)


def record_cron_fire(state: dict[str, Any], target: str, cron: str, when: datetime) -> None:
    state["cron_fired"][f"{target}|{cron}"] = _bucket(when)


def _prune_window(timestamps: list[str], now: datetime) -> list[str]:
    cutoff = (now - WAKE_WINDOW).isoformat()
    return [ts for ts in timestamps if ts >= cutoff]


def wake_count(state: dict[str, Any], agent_id: str, now: datetime) -> int:
    timestamps = state["wakes"].get(agent_id, [])
    fresh = _prune_window(timestamps, now)
    state["wakes"][agent_id] = fresh
    return len(fresh)


def should_skip_wake(state: dict[str, Any], agent_id: str, max_per_hour: int, now: datetime) -> bool:
    if max_per_hour <= 0:
        return False
    return wake_count(state, agent_id, now) >= max_per_hour


def record_wake(state: dict[str, Any], agent_id: str, now: datetime) -> None:
    timestamps = state["wakes"].setdefault(agent_id, [])
    timestamps.append(now.isoformat())
    state["wakes"][agent_id] = _prune_window(timestamps, now)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

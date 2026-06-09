"""Per-provider model-call throttle state (model resilience #83/#84).

File-backed at `state/model/throttle.json` so spacing + circuit-breaker state
survive across supervisor ticks and separate CLI processes (wall-clock seconds,
since monotonic clocks aren't comparable across processes). Per provider:

    {"providers": {"chatgpt": {"last_call": 1.7e9, "consecutive_429": 2,
                               "cooldown_until": 1.7e9}}}

Two mechanisms, both opt-in via config:
- **min spacing** (#83): refuse to fire a provider more often than
  `min_seconds_between_calls` — `due_wait` returns how long to sleep first.
- **circuit breaker** (#84): after N consecutive 429s a provider is parked for
  a cooldown so a sustained cap isn't hammered (and `call_model` falls straight
  to the fallback provider instead).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, read_json, write_json


def state_path(home: Path) -> Path:
    return Path(home) / "state" / "model" / "throttle.json"


def _load(home: Path) -> dict[str, dict[str, Any]]:
    path = state_path(home)
    if not path.exists():
        return {}
    try:
        raw = read_json(path)
    except (ValueError, OSError):
        return {}
    return dict(raw.get("providers") or {}) if isinstance(raw, dict) else {}


def _save(home: Path, providers: dict[str, dict[str, Any]]) -> None:
    path = state_path(home)
    ensure_dir(path.parent)
    write_json(path, {"providers": providers})


def _entry(providers: dict[str, dict[str, Any]], provider_id: str) -> dict[str, Any]:
    return providers.setdefault(provider_id, {})


def due_wait(home: Path, provider_id: str, min_seconds: float, *, now: float) -> float:
    """Seconds to wait before calling `provider_id` to honor the min spacing
    (0 when enough time has elapsed or spacing is disabled)."""
    if min_seconds <= 0:
        return 0.0
    last = _load(home).get(provider_id, {}).get("last_call")
    if last is None:
        return 0.0
    return max(0.0, float(min_seconds) - (now - float(last)))


def record_call(home: Path, provider_id: str, *, now: float) -> None:
    """Stamp a call time for the min-spacing window (call AFTER any due-wait)."""
    providers = _load(home)
    _entry(providers, provider_id)["last_call"] = now
    _save(home, providers)


def breaker_open(home: Path, provider_id: str, *, now: float) -> bool:
    """True while `provider_id` is parked in a 429 cooldown."""
    cooldown_until = _load(home).get(provider_id, {}).get("cooldown_until")
    return cooldown_until is not None and now < float(cooldown_until)


def note_rate_limited(home: Path, provider_id: str, *, now: float, threshold: int, cooldown: float) -> bool:
    """Record a 429 for `provider_id`. After `threshold` consecutive 429s, open
    the breaker for `cooldown` seconds. Returns True if the breaker is now open."""
    providers = _load(home)
    entry = _entry(providers, provider_id)
    entry["consecutive_429"] = int(entry.get("consecutive_429", 0)) + 1
    opened = entry["consecutive_429"] >= max(1, threshold) and cooldown > 0
    if opened:
        entry["cooldown_until"] = now + float(cooldown)
    _save(home, providers)
    return opened


def note_success(home: Path, provider_id: str) -> None:
    """A successful call clears the 429 streak + any open cooldown."""
    providers = _load(home)
    entry = providers.get(provider_id)
    if entry and (entry.get("consecutive_429") or entry.get("cooldown_until")):
        entry.pop("consecutive_429", None)
        entry.pop("cooldown_until", None)
        _save(home, providers)

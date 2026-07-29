"""Opt-in telemetry — roadmap "production needs" item 6, the last v1.0 gap.

Local-first rules, enforced by construction:

- **Default OFF.** Nothing is collected or sent until `jigga telemetry on`.
- **The payload is inspectable before and after opting in**: `jigga telemetry
  report` prints the EXACT JSON that would be sent. No surprises.
- **Counts, never content.** Error *type* frequencies from the audit log,
  config shape (number of agents/teams/capabilities), version, platform, and
  a random install id minted locally. Never: prompts, messages, secrets,
  file paths, agent names, event details, or anything typed by a human.
- Sent at most once/day from the supervisor heartbeat (marker-guarded,
  contained) to `telemetry.endpoint` — configurable, so self-hosters can
  point it at their own collector or nowhere.
"""

from __future__ import annotations

import json
import platform
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jigga.core.config import load_runtime_config
from jigga.core.io import read_json, read_yaml, write_json, write_yaml
from jigga.core.models import now_iso
from jigga.runtime.audit import new_id

DEFAULT_ENDPOINT = "https://telemetry.jigga.dev/v1/ingest"
_STATE = "state/telemetry.json"
_SEND_INTERVAL_HOURS = 24
# Only these event-type PREFIXES are counted — an explicit allowlist so a new
# event type never leaks into telemetry by default.
_COUNTED_PREFIXES = ("supervisor.", "agent.run.", "workflow.run.", "model.",
                     "channel.ingest_error", "capability.invocation.",
                     "recovery.", "reminder.", "egress.", "secret.denied")


def _config(home: Path) -> dict[str, Any]:
    return load_runtime_config(home).get("telemetry") or {}


def enabled(home: Path) -> bool:
    return bool(_config(home).get("enabled"))


def _state_path(home: Path) -> Path:
    return Path(home) / _STATE


def _state(home: Path) -> dict[str, Any]:
    path = _state_path(home)
    if path.exists():
        try:
            return read_json(path)
        except (OSError, ValueError):
            pass
    return {}


def install_id(home: Path) -> str:
    """Random, locally-minted, meaningless outside this install."""
    state = _state(home)
    if not state.get("install_id"):
        state["install_id"] = new_id("install")
        write_json(_state_path(home), state)
    return state["install_id"]


def set_enabled(home: Path, value: bool) -> None:
    config_path = Path(home) / "config.yaml"
    config = read_yaml(config_path) if config_path.exists() else {}
    config["telemetry"] = {**(config.get("telemetry") or {}), "enabled": value}
    write_yaml(config_path, config)


def build_payload(home: Path, *, window_hours: int = 24) -> dict[str, Any]:
    """The exact JSON a send would transmit. Counts only — see module doc."""
    from jigga import __version__ as version
    home = Path(home)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    counts: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    log = home / "logs" / "events.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines()[-20000:]:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("time", "") < cutoff:
                continue
            etype = str(event.get("type", ""))
            if etype.startswith(_COUNTED_PREFIXES):
                counts[etype] += 1
                if event.get("status") in ("error", "failed", "denied"):
                    errors[etype] += 1
    def _count_dir(name: str) -> int:
        d = home / name
        return sum(1 for p in d.glob("*.yaml")) if d.exists() else 0
    return {
        "schema": 1,
        "install_id": install_id(home),
        "sent_at": now_iso(),
        "version": version,
        "platform": {"system": platform.system(), "python": platform.python_version()},
        "shape": {"agents": _count_dir("agents"), "teams": _count_dir("teams"),
                  "workflows": _count_dir("workflows")},
        "window_hours": window_hours,
        "event_counts": dict(counts.most_common(40)),
        "error_counts": dict(errors.most_common(40)),
    }


def send(home: Path) -> dict[str, Any]:
    payload = build_payload(home)
    endpoint = str(_config(home).get("endpoint") or DEFAULT_ENDPOINT)
    request = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 — opt-in, configurable endpoint
        status = response.status
    state = _state(home)
    state["last_sent_at"] = now_iso()
    write_json(_state_path(home), state)
    return {"sent": True, "endpoint": endpoint, "status": status}


def maybe_send(home: Path) -> dict[str, Any] | None:
    """Supervisor hook: opt-in + at most once per day; never raises upward
    (caller contains). Returns the send result or None when skipped."""
    home = Path(home)
    if not enabled(home):
        return None
    last = _state(home).get("last_sent_at")
    if last:
        try:
            parsed = datetime.fromisoformat(last)
            if datetime.now(timezone.utc) - parsed < timedelta(hours=_SEND_INTERVAL_HOURS):
                return None
        except ValueError:
            pass
    return send(home)

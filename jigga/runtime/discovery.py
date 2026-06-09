"""Proactive workflow discovery (surfacing layer over runtime/inference.py).

`suggest_workflows` already mines the audit log for repeated work patterns and
drafts candidate workflows, but only when the CLI asks. This runs it on the
supervisor heartbeat (at most once per `interval_hours`, marker-guarded like
memory compaction) and *surfaces* each NEW high-confidence suggestion:

- an audit event `workflow.suggested` (the file-first surface jiggaview + the
  Runs page read),
- a push to the user's default chat channel (the same config-resolved
  owner-delivery path `notifications.send` uses), and
- optionally a desktop notification.

A `surfaced` set (`state/workflows/surfaced.json`) dedups so the same suggestion
isn't re-notified every cycle, and already-applied suggestions
(`workflows/<id>.yaml` exists) are skipped.

Config (`config.yaml`):
    workflows:
      discovery:
        enabled: true
        interval_hours: 24
        min_count: 2
        min_confidence: 0.7
        notify_channel: true
        notify_desktop: false
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jigga.core.config import load_runtime_config, resolve_default_agent
from jigga.core.io import ensure_dir, read_json, write_json
from jigga.runtime.audit import append_event
from jigga.runtime.inference import suggest_workflows


def _orchestrator(home: Path) -> str | None:
    """The agent that runs a suggested workflow's `task.assign` steps — the
    default/chief agent (it holds team-orchestration)."""
    return resolve_default_agent(Path(home) / "agents")

_MARKER = ".discovery.json"
_DEFAULTS = {"enabled": True, "interval_hours": 24, "min_count": 2,
             "min_confidence": 0.7, "notify_channel": True, "notify_desktop": False}


def _config(home: Path) -> dict[str, Any]:
    raw = (load_runtime_config(home).get("workflows") or {}).get("discovery") or {}
    return {**_DEFAULTS, **raw}


def _marker_path(home: Path) -> Path:
    return Path(home) / "state" / _MARKER


def _surfaced_path(home: Path) -> Path:
    return Path(home) / "state" / "workflows" / "surfaced.json"


def _load_surfaced(home: Path) -> set[str]:
    path = _surfaced_path(home)
    if not path.exists():
        return set()
    try:
        return set(read_json(path).get("ids") or [])
    except (ValueError, OSError):
        return set()


def _save_surfaced(home: Path, ids: set[str]) -> None:
    path = _surfaced_path(home)
    ensure_dir(path.parent)
    write_json(path, {"ids": sorted(ids)})


def _parse(ts: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _notify_channel(home: Path, logs_dir: Path, suggestion: dict[str, Any]) -> None:
    """Push a suggestion to the user's default channel (config-resolved owner
    conversation — same path notifications.send uses). Best-effort."""
    from jigga.runtime.channels import ADAPTERS, owner_conversation

    target = owner_conversation(home)
    if target is None:
        return
    channel, conversation_id = target
    pct = round(float(suggestion.get("confidence", 0)) * 100)
    text = (f"I noticed a repeated pattern — {suggestion.get('name')} ({pct}% confidence). "
            f"Create a workflow? Open the Workflows page, or run: "
            f"jigga workflow apply {suggestion['id']} --approve")
    try:
        ADAPTERS[channel].send(home, conversation_id=conversation_id, text=text)
        append_event(logs_dir, "notification.channel_delivered", channel=channel,
                     title="workflow.suggested")
    except Exception as exc:  # noqa: BLE001 — channel push is best-effort; the audit event still surfaced it
        append_event(logs_dir, "notification.channel_failed", status="error",
                     channel=channel, error=str(exc))


def _notify_desktop(suggestion: dict[str, Any]) -> None:
    from jigga.runtime.notifications import NotificationRequest, send_notification

    send_notification(NotificationRequest(
        title="JIGGA — workflow suggestion",
        body=f"Repeated pattern: {suggestion.get('name')}. Review in the Workflows page.",
    ))


def open_suggestions(home: Path, logs_dir: Path, min_count: int = 2) -> list[dict[str, Any]]:
    """Suggestions that haven't been turned into a workflow yet (the UI list).
    Each gains `applied: False` here; the CLI/UI can mark applied ones too."""
    workflows_dir = Path(home) / "workflows"
    out: list[dict[str, Any]] = []
    for suggestion in suggest_workflows(logs_dir, min_count=min_count, orchestrator=_orchestrator(home)):
        applied = (workflows_dir / f"{suggestion['id']}.yaml").exists()
        out.append({**suggestion, "applied": applied})
    return out


def maybe_surface_suggestions(home: Path, logs_dir: Path, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Run discovery at most once per `interval_hours` and surface NEW
    high-confidence suggestions (audit event + chat/desktop notify). Returns a
    summary if it ran, else None. Called on the supervisor heartbeat."""
    home = Path(home)
    cfg = _config(home)
    if not cfg.get("enabled", True):
        return None
    now = now or datetime.now(timezone.utc)
    marker = _marker_path(home)
    if marker.exists():
        try:
            last = _parse(json.loads(marker.read_text(encoding="utf-8")).get("last_run"))
        except (OSError, ValueError):
            last = None
        if last is not None and now - last < timedelta(hours=float(cfg["interval_hours"])):
            return None

    surfaced = _load_surfaced(home)
    workflows_dir = home / "workflows"
    min_confidence = float(cfg["min_confidence"])
    new_ids: list[str] = []
    for suggestion in suggest_workflows(logs_dir, min_count=int(cfg["min_count"]),
                                        orchestrator=_orchestrator(home)):
        sid = suggestion["id"]
        if (float(suggestion.get("confidence", 0)) < min_confidence
                or sid in surfaced
                or (workflows_dir / f"{sid}.yaml").exists()):
            continue
        append_event(logs_dir, "workflow.suggested", status="ask",
                     suggestion_id=sid, name=suggestion.get("name"),
                     confidence=suggestion.get("confidence"),
                     evidence_count=suggestion.get("evidence_count"),
                     step_count=suggestion.get("step_count"),
                     modal_hour_utc=suggestion.get("modal_hour_utc"))
        if cfg.get("notify_channel", True):
            _notify_channel(home, logs_dir, suggestion)
        if cfg.get("notify_desktop", False):
            try:
                _notify_desktop(suggestion)
            except Exception:  # noqa: BLE001 — desktop notify is best-effort
                pass
        surfaced.add(sid)
        new_ids.append(sid)

    if new_ids:
        _save_surfaced(home, surfaced)
    ensure_dir(marker.parent)
    write_json(marker, {"last_run": now.isoformat()})
    return {"surfaced": new_ids}

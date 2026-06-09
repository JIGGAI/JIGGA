"""Proactive workflow discovery — the supervisor surfaces NEW high-confidence
suggestions (audit event + channel push), once per interval, deduped."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime import channels, discovery


def _seed_pattern(home: Path, *, sessions: int = 3) -> None:
    """Write a repeated 2-step shape across N time-separated sessions so the
    suggestion clears the confidence threshold."""
    base = datetime(2026, 6, 8, 9, 0, 0, tzinfo=timezone.utc)
    lines = []
    for s in range(sessions):
        t0 = base + timedelta(hours=s)
        for i, (agent, title) in enumerate([("researcher", "gather sources"), ("writer", "draft summary")]):
            lines.append(json.dumps({
                "id": f"e{s}{i}", "time": (t0 + timedelta(seconds=i * 30)).isoformat(),
                "type": "agent.task_completed", "status": "ok",
                "details": {"agent_id": agent, "title": title},
            }))
    (home / "logs" / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _set_discovery(paths, cfg: dict) -> None:
    config = read_yaml(paths.config)
    config.setdefault("workflows", {})["discovery"] = cfg
    write_yaml(paths.config, config)


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _capture_channel(monkeypatch) -> list[str]:
    sent: list[str] = []

    class _Adapter:
        def send(self, home, *, conversation_id, text):
            sent.append(text)
            return {"delivered": True}

    monkeypatch.setattr(channels, "owner_conversation", lambda home, channel=None: ("telegram", "123"))
    monkeypatch.setattr(channels, "ADAPTERS", {"telegram": _Adapter()})
    return sent


def test_surfaces_new_suggestion_with_audit_and_channel_push(tmp_path: Path, monkeypatch):
    paths = init_runtime(tmp_path)
    _seed_pattern(tmp_path)
    sent = _capture_channel(monkeypatch)

    result = discovery.maybe_surface_suggestions(tmp_path, paths.logs)
    assert result and len(result["surfaced"]) == 1
    sid = result["surfaced"][0]

    suggested = [e for e in _events(paths) if e["type"] == "workflow.suggested"]
    assert len(suggested) == 1 and suggested[0]["details"]["suggestion_id"] == sid
    assert len(sent) == 1 and sid in sent[0]  # pushed to the owner's channel
    # recorded so it won't re-surface
    assert sid in json.loads((tmp_path / "state" / "workflows" / "surfaced.json").read_text())["ids"]


def test_marker_skips_within_interval(tmp_path: Path, monkeypatch):
    paths = init_runtime(tmp_path)
    _seed_pattern(tmp_path)
    _capture_channel(monkeypatch)
    assert discovery.maybe_surface_suggestions(tmp_path, paths.logs)["surfaced"]
    # second run inside the 24h interval → no-op (None)
    assert discovery.maybe_surface_suggestions(tmp_path, paths.logs) is None


def test_already_surfaced_not_re_emitted_after_interval(tmp_path: Path, monkeypatch):
    paths = init_runtime(tmp_path)
    _seed_pattern(tmp_path)
    _capture_channel(monkeypatch)
    discovery.maybe_surface_suggestions(tmp_path, paths.logs)
    later = datetime.now(timezone.utc) + timedelta(hours=48)
    result = discovery.maybe_surface_suggestions(tmp_path, paths.logs, now=later)
    assert result["surfaced"] == []  # interval elapsed, but the id is already surfaced
    assert sum(1 for e in _events(paths) if e["type"] == "workflow.suggested") == 1


def test_below_confidence_threshold_skipped(tmp_path: Path, monkeypatch):
    paths = init_runtime(tmp_path)
    _seed_pattern(tmp_path)
    _capture_channel(monkeypatch)
    _set_discovery(paths, {"min_confidence": 0.99})  # 0.8 suggestion is below this
    result = discovery.maybe_surface_suggestions(tmp_path, paths.logs)
    assert result["surfaced"] == []
    assert not any(e["type"] == "workflow.suggested" for e in _events(paths))


def test_notify_channel_false_sends_nothing(tmp_path: Path, monkeypatch):
    paths = init_runtime(tmp_path)
    _seed_pattern(tmp_path)
    sent = _capture_channel(monkeypatch)
    _set_discovery(paths, {"notify_channel": False})
    result = discovery.maybe_surface_suggestions(tmp_path, paths.logs)
    assert result["surfaced"] and sent == []  # audit-only, no channel push


def test_open_suggestions_marks_applied(tmp_path: Path, monkeypatch):
    paths = init_runtime(tmp_path)
    _seed_pattern(tmp_path)
    [s] = discovery.open_suggestions(tmp_path, paths.logs)
    assert s["applied"] is False
    (tmp_path / "workflows" / f"{s['id']}.yaml").write_text("id: x\nname: x\n", encoding="utf-8")
    [s2] = discovery.open_suggestions(tmp_path, paths.logs)
    assert s2["applied"] is True

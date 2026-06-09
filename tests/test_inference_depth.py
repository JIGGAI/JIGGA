from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.runtime.inference import apply_suggestion, suggest_workflows


def _write_events(logs_dir: Path, events: list[dict]) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    with (logs_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True) + "\n")


def _event(when: datetime, *, agent_id: str | None = None, workflow: str | None = None, title: str, etype: str = "agent.task_completed") -> dict:
    details: dict = {"title": title}
    if agent_id is not None:
        details["agent_id"] = agent_id
    if workflow is not None:
        details["workflow"] = workflow
    return {
        "id": f"evt_{when.isoformat()}",
        "time": when.isoformat(),
        "type": etype,
        "status": "ok",
        "details": details,
    }


def test_inference_detects_multi_step_session_shape(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    base = datetime(2026, 5, 25, 7, 30, tzinfo=timezone.utc)
    # Three sessions on three different days, each with the same 3-step shape
    # (calendar → email → summarize), separated by >5 min so they form distinct sessions.
    for day_offset in range(3):
        when = base + timedelta(days=day_offset)
        _write_events(
            paths.logs,
            [
                _event(when, agent_id="daily_briefing_agent", title="read_calendar"),
                _event(when + timedelta(seconds=10), agent_id="daily_briefing_agent", title="read_email"),
                _event(when + timedelta(seconds=20), agent_id="daily_briefing_agent", title="summarize_day"),
            ],
        )

    suggestions = suggest_workflows(paths.logs)
    multi_step = [s for s in suggestions if s["step_count"] == 3]
    assert multi_step, f"Expected a multi-step suggestion, got: {[s['step_count'] for s in suggestions]}"
    top = multi_step[0]
    assert top["evidence_count"] == 3
    # Runnable steps: each re-dispatches the observed task via task.assign; the
    # inferred title rides on the step input.
    steps = top["workflow"]["steps"]
    assert [step["action"] for step in steps] == ["task.assign", "task.assign", "task.assign"]
    assert [step["input"]["title"] for step in steps] == ["read_calendar", "read_email", "summarize_day"]
    assert all(step["input"]["assignee"] == "daily_briefing_agent" for step in steps)
    # All sessions started at 07:30 UTC → modal hour hint set
    assert top["modal_hour_utc"] == 7
    assert "hint" in top


def test_inference_collapses_consecutive_identical_events(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    base = datetime(2026, 5, 25, 9, 0, tzinfo=timezone.utc)
    # Two sessions of THE SAME single event repeated; collapse to one step each.
    for day_offset in range(2):
        when = base + timedelta(days=day_offset)
        _write_events(
            paths.logs,
            [
                _event(when, agent_id="briefing", title="Summarize"),
                _event(when + timedelta(seconds=1), agent_id="briefing", title="Summarize"),
                _event(when + timedelta(seconds=2), agent_id="briefing", title="Summarize"),
            ],
        )
    suggestions = suggest_workflows(paths.logs)
    assert all(s["step_count"] == 1 for s in suggestions)


def test_inference_time_pattern_only_when_majority_clusters(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    # Two sessions at different hours — no clear modal hour.
    _write_events(paths.logs, [_event(datetime(2026, 5, 25, 3, 0, tzinfo=timezone.utc), agent_id="agent_a", title="ping")])
    _write_events(paths.logs, [_event(datetime(2026, 5, 25, 14, 0, tzinfo=timezone.utc), agent_id="agent_a", title="ping")])
    suggestions = suggest_workflows(paths.logs)
    assert suggestions
    assert suggestions[0]["modal_hour_utc"] is None
    assert "hint" not in suggestions[0]


def test_apply_suggestion_returns_already_applied_on_reapply(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    base = datetime(2026, 5, 25, 7, 30, tzinfo=timezone.utc)
    for day_offset in range(2):
        when = base + timedelta(days=day_offset)
        _write_events(paths.logs, [_event(when, agent_id="briefing", title="Summarize")])

    suggestions = suggest_workflows(paths.logs)
    suggestion_id = suggestions[0]["id"]
    applied = apply_suggestion(paths.workflows, suggestion_id, paths.logs, approve=True)
    assert applied["status"] == "applied"
    again = apply_suggestion(paths.workflows, suggestion_id, paths.logs, approve=True)
    assert again["status"] == "already_applied"
    assert again["path"] == applied["path"]

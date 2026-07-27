"""One-shot reminders: due parsing, file-first records, exactly-once supervisor
firing into the task queue, and the capability handler."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime import reminders as mod
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.supervisor import supervisor_tick
from jigga.runtime.tasks import list_tasks

_NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def test_parse_due_iso_and_offsets() -> None:
    assert mod.parse_due("2026-07-28T17:00:00+00:00", None).hour == 17
    # Naive ISO treated as UTC.
    assert mod.parse_due("2026-07-28T17:00:00", None).tzinfo is not None
    assert mod.parse_due(None, "30m", now=_NOW) == _NOW + timedelta(minutes=30)
    assert mod.parse_due(None, "2h", now=_NOW) == _NOW + timedelta(hours=2)
    assert mod.parse_due(None, "1d", now=_NOW) == _NOW + timedelta(days=1)
    with pytest.raises(ValueError, match="input.at"):
        mod.parse_due(None, "next tuesday")


def test_create_and_list(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    record = mod.create_reminder(paths.home, message="Call Alice", offset="1h", now=_NOW)
    assert (paths.home / "reminders" / f"{record['id']}.json").exists()
    assert [r["id"] for r in mod.list_reminders(paths.home)] == [record["id"]]
    with pytest.raises(ValueError, match="input.message"):
        mod.create_reminder(paths.home, message="  ", offset="1h")


def test_fire_due_creates_task_exactly_once(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    mod.create_reminder(paths.home, message="Send the report", offset="1h", now=_NOW,
                        agent="daily_briefing_agent")
    mod.create_reminder(paths.home, message="Not yet", offset="3h", now=_NOW)

    fired = mod.fire_due_reminders(paths.home, paths.logs, paths.tasks, paths.agents,
                                   now=_NOW + timedelta(hours=2))
    assert len(fired) == 1 and fired[0]["message"] == "Send the report"
    tasks = [t for t in list_tasks(paths.tasks) if (t.metadata or {}).get("reminder")]
    assert len(tasks) == 1 and tasks[0].assignee == "daily_briefing_agent"
    assert "Send the report" in tasks[0].description

    # Second sweep: nothing re-fires.
    again = mod.fire_due_reminders(paths.home, paths.logs, paths.tasks, paths.agents,
                                   now=_NOW + timedelta(hours=2))
    assert again == []
    assert mod.list_reminders(paths.home)[0]["message"] == "Not yet"  # future one still pending


def test_unknown_agent_falls_back_to_default(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    from jigga.core.config import resolve_default_agent

    default = resolve_default_agent(paths.agents)
    mod.create_reminder(paths.home, message="Orphan", offset="1m", now=_NOW, agent="gone_agent")
    fired = mod.fire_due_reminders(paths.home, paths.logs, paths.tasks, paths.agents,
                                   now=_NOW + timedelta(hours=1))
    if default is None:
        assert fired == []  # unroutable stays pending, audited
        assert mod.list_reminders(paths.home)
    else:
        assert fired[0]["task_id"]


def test_supervisor_tick_sweeps_reminders(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    record = mod.create_reminder(paths.home, message="Tick me", offset="1s",
                                 now=datetime.now(timezone.utc) - timedelta(minutes=5),
                                 agent="daily_briefing_agent")
    supervisor_tick(paths.home)
    stored = mod.list_reminders(paths.home, include_fired=True)
    assert stored[0]["id"] == record["id"] and stored[0]["status"] == "fired"


def test_handler_actions(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = AgentConfig(id="chief", name="Chief", role="assistant")
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    created = mod.reminders_handler(WorkflowStep(id="s", action="remind.at"), None,
                                    {"message": "Stretch", "in": "30m"}, {}, runtime)
    assert created["status"] == "pending" and created["created_by"] == "chief"
    assert created["agent"] == "chief"  # defaults to the creating agent
    listed = mod.reminders_handler(WorkflowStep(id="s", action="remind.list"), None, {}, {}, runtime)
    assert [r["id"] for r in listed["reminders"]] == [created["id"]]


def test_capability_registered_low_risk() -> None:
    registry = CapabilityRegistry.load()
    capability = registry.resolve_action("remind.at")
    assert capability is not None and capability.name == "reminders"
    assert capability.risk_level == "low"

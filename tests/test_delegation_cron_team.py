from __future__ import annotations

from datetime import datetime

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.runtime.scheduler import due_events
from jigga.runtime.supervisor import supervisor_tick
from jigga.runtime.tasks import create_task, list_tasks
from jigga.runtime.team import run_team


def test_scheduler_finds_agent_and_workflow_events(tmp_path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    events = due_events(paths.agents, paths.workflows, now=datetime(2026, 5, 25, 7, 30))
    event_types = {event.type for event in events}
    assert "cron.tick" in event_types
    assert "workflow.schedule_due" in event_types


def test_supervisor_creates_scheduled_tasks_and_runs_pending_tasks(tmp_path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    create_task(paths.tasks, "Delegated task", assignee="daily_briefing_agent", metadata={"delegated_by": "test"})
    result = supervisor_tick(tmp_path)
    assert "daily_briefing_agent" in result["targets"]
    assert all(task.state == "completed" for task in list_tasks(paths.tasks) if task.assignee == "daily_briefing_agent")


def test_team_runtime_creates_coordination_task_and_runs_defaults(tmp_path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    result = run_team(paths, "personal_admin_team")
    assert result["team_id"] == "personal_admin_team"
    assert result["created_tasks"]
    assert result["workflow_runs"]


def test_cli_team_and_scheduler_smoke(tmp_path, capsys) -> None:
    assert main(["--home", str(tmp_path), "init", "--examples"]) == 0
    assert main(["--home", str(tmp_path), "scheduler", "due", "--at", "2026-05-25T07:30:00"]) == 0
    assert "event" in capsys.readouterr().out
    assert main(["--home", str(tmp_path), "team", "run", "personal_admin_team"]) == 0
    assert "personal_admin_team" in capsys.readouterr().out

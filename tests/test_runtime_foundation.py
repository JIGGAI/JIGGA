from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_teams
from jigga.runtime.agent import run_agent
from jigga.runtime.supervisor import supervisor_tick
from jigga.runtime.tasks import create_task, list_tasks


def test_init_loads_example_agents_and_teams(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    assert paths.config.exists()
    assert paths.state.exists()
    assert (paths.memory / "raw").is_dir()
    assert "daily_briefing_agent" in load_agents(paths.agents)
    assert "personal_admin_team" in load_teams(paths.teams)


def test_agent_runner_processes_pending_tasks(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    task = create_task(paths.tasks, "Prepare a dry-run briefing", assignee="daily_briefing_agent")
    result = run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "daily_briefing_agent")
    assert result["agent_id"] == "daily_briefing_agent"
    assert result["processed_tasks"][0]["id"] == task.id
    assert list_tasks(paths.tasks)[0].state == "completed"
    assert (paths.logs / "events.jsonl").exists()


def test_supervisor_tick_wakes_assigned_agent(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    create_task(paths.tasks, "Wake from supervisor", assignee="daily_briefing_agent")
    result = supervisor_tick(tmp_path)
    assert result["targets"] == ["daily_briefing_agent"]
    assert list_tasks(paths.tasks)[0].state == "completed"


def test_cli_smoke(tmp_path: Path, capsys) -> None:
    assert main(["--home", str(tmp_path), "init", "--examples"]) == 0
    assert main(["--home", str(tmp_path), "state"]) == 0
    capsys.readouterr()

    assert main(["--home", str(tmp_path), "task", "create", "--title", "Test task", "--assignee", "daily_briefing_agent"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["title"] == "Test task"

    assert main(["--home", str(tmp_path), "supervisor", "tick"]) == 0
    assert main(["--home", str(tmp_path), "task", "list"]) == 0
    assert "completed" in capsys.readouterr().out

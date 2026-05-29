from __future__ import annotations

from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.runtime.agent import run_agent
from jigga.runtime.tasks import create_task
from jigga.runtime.inference import suggest_workflows
from jigga.runtime.workflow import run_workflow


def test_actual_agent_runs_feed_workflow_inference(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    create_task(paths.tasks, "Summarize inbox", assignee="daily_briefing_agent")
    run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "daily_briefing_agent")
    create_task(paths.tasks, "Summarize inbox", assignee="daily_briefing_agent")
    run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "daily_briefing_agent")

    suggestions = suggest_workflows(paths.logs)
    assert suggestions
    assert suggestions[0]["workflow"]["steps"][0]["agent"] == "daily_briefing_agent"
    assert suggestions[0]["workflow"]["steps"][0]["approval"] == "required"


def test_completed_workflow_runs_feed_workflow_inference(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    run_workflow(paths.home, paths.logs, paths.workflows, paths.agents, paths.memory, "morning_day_summary")
    run_workflow(paths.home, paths.logs, paths.workflows, paths.agents, paths.memory, "morning_day_summary")

    suggestions = suggest_workflows(paths.logs)
    assert any(suggestion["workflow"]["steps"][0]["agent"] == "morning_day_summary" for suggestion in suggestions)


def test_supervisor_start_cli_runs_bounded_loop(tmp_path: Path, capsys) -> None:
    assert main(["--home", str(tmp_path), "init", "--examples"]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "supervisor", "start", "--interval-seconds", "0", "--max-ticks", "1"]) == 0
    output = capsys.readouterr().out
    assert "tick_count" in output
    assert "stopped" in output

from __future__ import annotations

from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_memory_scopes, load_workflows
from jigga.runtime.memory import build_context_package, inspect_memory
from jigga.runtime.workflow import plan_workflow, run_workflow


def test_memory_scopes_are_copied_and_loaded(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    scopes = load_memory_scopes(paths.memory)
    assert "manager_view" in scopes
    assert "task_only" in scopes
    inspected = inspect_memory(paths.memory)
    assert inspected["layers"]["raw"].endswith("memory/raw")
    assert "manager_view" in inspected["scopes"]


def test_context_package_respects_scope_includes(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    (paths.memory / "summaries").mkdir(parents=True, exist_ok=True)
    (paths.memory / "summaries" / "user_goals.md").write_text("Ship JIGGA MVP", encoding="utf-8")
    package = build_context_package(paths.memory, "manager_view")
    assert package["scope"]["id"] == "manager_view"
    assert any(item.get("content") == "Ship JIGGA MVP" for item in package["included"])


def test_workflow_plan_and_run(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    workflow = load_workflows(paths.workflows)["morning_day_summary"]
    plan = plan_workflow(workflow, load_agents(paths.agents))
    assert plan["can_run"] is True
    assert len(plan["steps"]) == 4

    result = run_workflow(paths, "morning_day_summary")
    assert result["status"] == "completed"
    assert Path(result["run_dir"], "run.json").exists()
    assert result["memory_artifact"] is not None
    assert Path(result["memory_artifact"]).exists()


def test_workflow_with_required_approval_is_blocked(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    workflow = load_workflows(paths.workflows)["social_content_syndication"]
    plan = plan_workflow(workflow, load_agents(paths.agents))
    assert plan["can_run"] is False
    assert any(step["policy"]["status"] == "needs_approval" for step in plan["steps"])


def test_cli_memory_and_workflow_smoke(tmp_path: Path, capsys) -> None:
    assert main(["--home", str(tmp_path), "init", "--examples"]) == 0
    assert main(["--home", str(tmp_path), "memory", "inspect"]) == 0
    assert "manager_view" in capsys.readouterr().out
    assert main(["--home", str(tmp_path), "workflow", "plan", "morning_day_summary"]) == 0
    assert "Plan: runnable" in capsys.readouterr().out
    assert main(["--home", str(tmp_path), "workflow", "run", "morning_day_summary"]) == 0
    assert "completed" in capsys.readouterr().out

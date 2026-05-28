from __future__ import annotations

from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.runtime.audit import append_event
from jigga.runtime.inference import apply_suggestion, suggest_workflows
from jigga.runtime.plan_apply import apply_runtime, plan_runtime, validate_runtime_configs


def test_plan_apply_tracks_runtime_config_snapshot(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    first = plan_runtime(paths)
    assert first["changes"]
    assert first["status"] == "needs_approval"

    blocked = apply_runtime(paths)
    assert blocked["status"] == "needs_approval"

    applied = apply_runtime(paths, approve=True)
    assert applied["status"] == "applied"
    second = plan_runtime(paths)
    assert second["changes"] == []
    assert second["status"] == "ready"


def test_validate_runtime_configs_loads_all_config_kinds(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    result = validate_runtime_configs(paths)
    assert "daily_briefing_agent" in result["agents"]
    assert "personal_admin_team" in result["teams"]
    assert "morning_day_summary" in result["workflows"]
    assert "manager_view" in result["memory_scopes"]


def test_workflow_inference_suggests_and_requires_approval_to_apply(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    append_event(paths.logs, "agent.task_completed", agent_id="daily_briefing_agent", title="Summarize inbox")
    append_event(paths.logs, "agent.task_completed", agent_id="daily_briefing_agent", title="Summarize inbox")

    suggestions = suggest_workflows(paths.logs)
    assert suggestions
    suggestion_id = suggestions[0]["id"]
    assert suggestions[0]["workflow"]["status"] == "suggested"
    assert suggestions[0]["workflow"]["steps"][0]["approval"] == "required"

    pending = apply_suggestion(paths.workflows, suggestion_id, paths.logs)
    assert pending["status"] == "needs_approval"

    applied = apply_suggestion(paths.workflows, suggestion_id, paths.logs, approve=True)
    assert applied["status"] == "applied"
    assert Path(applied["path"]).exists()


def test_phase4_cli_smoke(tmp_path: Path, capsys) -> None:
    assert main(["--home", str(tmp_path), "init", "--examples"]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "validate", "--json"]) == 0
    assert "daily_briefing_agent" in capsys.readouterr().out
    assert main(["--home", str(tmp_path), "plan", "--json"]) == 0
    assert "changes" in capsys.readouterr().out
    assert main(["--home", str(tmp_path), "apply", "--approve"]) == 0
    assert "applied" in capsys.readouterr().out

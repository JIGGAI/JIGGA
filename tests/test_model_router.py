from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.config import load_agents
from jigga.runtime.model_router import (
    DuplicateModelInputError,
    ModelCallItem,
    build_task_model_request,
    call_model,
    validate_unique_input_items,
)
from jigga.runtime.tasks import create_task, list_tasks
from jigga.runtime.agent import run_agent


def test_model_router_dry_run_executes_without_external_credentials(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = load_agents(paths.agents)["daily_briefing_agent"]
    request = build_task_model_request(
        agent,
        {"id": "task_1", "title": "Brief me", "description": "Summarize the morning."},
    )
    result = call_model(paths.home, paths.logs, request)
    assert result.status == "ok"
    assert result.provider == "dry_run"
    assert result.dry_run is True
    assert "Brief me" in result.content


def test_model_router_rejects_duplicate_input_item_ids(tmp_path: Path) -> None:
    init_runtime(tmp_path, examples=True)
    items = [
        ModelCallItem(id="msg_289", role="user", content="first"),
        ModelCallItem(id="msg_289", role="user", content="second"),
    ]
    try:
        validate_unique_input_items(items)
    except DuplicateModelInputError as exc:
        assert "msg_289" in str(exc)
    else:
        raise AssertionError("duplicate input ids should be rejected before provider calls")


def test_agent_runner_uses_model_router(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    task = create_task(paths.tasks, "Prepare a dry-run briefing", assignee="daily_briefing_agent")
    result = run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "daily_briefing_agent")
    assert result["processed_tasks"][0]["id"] == task.id
    assert list_tasks(paths.tasks)[0].state == "completed"
    artifact = next((paths.home / "runs" / "agents" / "daily_briefing_agent" / result["id"]).glob(f"{task.id}.json"))
    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert data["model"]["provider"] == "dry_run"
    assert "Dry-run model response" in data["result"]


def test_model_test_cli(tmp_path: Path, capsys) -> None:
    assert main(["--home", str(tmp_path), "init", "--examples"]) == 0
    capsys.readouterr()
    assert main([
        "--home",
        str(tmp_path),
        "model",
        "test",
        "daily_briefing_agent",
        "--prompt",
        "Say hello",
        "--dry-run",
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["provider"] == "dry_run"
    assert output["dry_run"] is True

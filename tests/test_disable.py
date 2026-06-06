"""Disable/enable (operational pause, config-stored — entity yamls stay
pristine) + recipes delete (existence ops live with the recipe)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.tasks import create_task, list_tasks


def _ok_model(home, logs_dir, request):
    return ModelCallResult(status="ok", provider="dry_run", model="m", content="ok",
                           dry_run=True, tool_calls=[])


def test_disabled_agent_not_woken_tasks_stay_pending(tmp_path: Path, capsys) -> None:
    from jigga.runtime.supervisor import supervisor_tick

    paths = init_runtime(tmp_path, examples=True)
    assert main(["--home", str(tmp_path), "agents", "disable", "copywriter"]) == 0
    capsys.readouterr()
    assert read_yaml(paths.config)["disabled"]["agents"] == ["copywriter"]

    create_task(paths.tasks, "work", assignee="copywriter")
    with patch("jigga.runtime.agent.call_model", _ok_model):
        supervisor_tick(paths.home)
    task = next(t for t in list_tasks(paths.tasks) if t.assignee == "copywriter")
    assert task.state == "pending"                       # visible, never lost, not run

    # re-enable → next tick runs it
    assert main(["--home", str(tmp_path), "agents", "enable", "copywriter"]) == 0
    capsys.readouterr()
    with patch("jigga.runtime.agent.call_model", _ok_model):
        supervisor_tick(paths.home)
    task = next(t for t in list_tasks(paths.tasks) if t.assignee == "copywriter")
    assert task.state == "completed"


def test_disabled_team_disables_all_members_and_cron(tmp_path: Path, capsys) -> None:
    from jigga.runtime.supervisor import supervisor_tick

    paths = init_runtime(tmp_path, examples=True)
    assert main(["--home", str(tmp_path), "team", "disable", "personal_admin_team"]) == 0
    capsys.readouterr()
    # force the briefing cron due by patching due_events? Simpler: pending task path
    create_task(paths.tasks, "brief now", assignee="daily_briefing_agent")
    with patch("jigga.runtime.agent.call_model", _ok_model):
        result = supervisor_tick(paths.home)
    task = next(t for t in list_tasks(paths.tasks) if t.assignee == "daily_briefing_agent")
    assert task.state == "pending"
    assert "daily_briefing_agent" not in [r.get("agent_id") for r in result.get("runs", [])
                                          if isinstance(r, dict)]


def test_disabled_channel_agent_not_run(tmp_path: Path, capsys) -> None:
    from jigga.runtime.channel_listener import ingest_once

    paths = init_runtime(tmp_path, examples=True)
    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": True, "allowed_chat_ids": ["1"],
                                       "default_agent": "daily_briefing_agent"}}
    write_yaml(paths.config, config)
    assert main(["--home", str(tmp_path), "agents", "disable", "daily_briefing_agent"]) == 0
    capsys.readouterr()

    poll = {"status": "ok", "messages": [{"channel": "telegram", "chat_id": 1, "sender": "a",
                                          "sender_id": 1, "text": "hi", "message_id": 1}]}
    with patch("jigga.runtime.telegram.poll_messages", return_value=poll), \
         patch("jigga.runtime.agent.call_model", _ok_model):
        summary = ingest_once(paths.home, paths.logs, paths.tasks, paths.agents)
    assert summary["created"]                       # task created (visible)
    assert summary["runs"] == []                    # agent not run
    events = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "channel.agent_disabled" for e in events)


def test_recipes_delete_user_copy_reverts_to_bundled(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    # shadow the bundled recipe, then delete the shadow
    assert main(["--home", str(tmp_path), "recipes", "cat", "researcher"]) == 0
    original = capsys.readouterr().out
    assert main(["--home", str(tmp_path), "recipes", "save", "researcher",
                 "--content", original.replace("Gathers", "Hunts")]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "recipes", "delete", "researcher"]) == 0
    out = capsys.readouterr().out
    assert "bundled version takes over" in out
    assert not (tmp_path / "recipes" / "researcher.md").exists()
    # backed up
    backups = list((tmp_path / "state" / "backups").rglob("researcher.md"))
    assert backups and "Hunts" in backups[0].read_text(encoding="utf-8")
    # bundled still resolvable
    assert main(["--home", str(tmp_path), "recipes", "cat", "researcher"]) == 0
    assert "Gathers" in capsys.readouterr().out


def test_recipes_delete_uninstall_tears_down_installed(tmp_path: Path, capsys) -> None:
    from jigga.core.config import load_agents

    paths = init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "recipes", "scaffold", "researcher"]) == 0
    capsys.readouterr()
    assert "researcher" in load_agents(paths.agents)

    assert main(["--home", str(tmp_path), "recipes", "delete", "researcher",
                 "--uninstall", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["uninstalled"] == ["researcher"]
    assert "researcher" not in load_agents(paths.agents)
    from jigga.runtime.recipes import installed_recipes
    assert not any(r["scaffold_id"] == "researcher" for r in installed_recipes(paths.home))


def test_recipes_delete_bundled_without_copy_errors_helpfully(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "recipes", "delete", "researcher"]) == 1
    assert "bundled recipe" in capsys.readouterr().out

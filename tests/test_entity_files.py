"""Agent/team file surfaces + recipe markdown editing — the CLI backends for
jiggaview's Files tabs and the team editor's Recipe tab (ClawKitchen parity)."""

from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime


def test_agent_files_lists_required_and_missing(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path, examples=True)
    assert main(["--home", str(tmp_path), "agents", "files", "daily_briefing_agent", "--json"]) == 0
    files = {f["name"]: f for f in json.loads(capsys.readouterr().out)}
    assert files["SOUL.md"]["required"] and not files["SOUL.md"]["missing"]      # scaffolded
    assert files["MEMORY.md"]["required"] and not files["MEMORY.md"]["missing"]
    assert not files["TOOLS.md"]["required"] and files["TOOLS.md"]["missing"]    # optional, absent


def test_agent_file_get_set_roundtrip_and_audit(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path, examples=True)
    assert main(["--home", str(tmp_path), "agents", "file", "set", "daily_briefing_agent",
                 "SOUL.md", "--content", "# SOUL\nBe crisp."]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "agents", "file", "get", "daily_briefing_agent",
                 "SOUL.md"]) == 0
    assert capsys.readouterr().out == "# SOUL\nBe crisp."

    events = [json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    edited = [e for e in events if e["type"] == "workspace.file_edited"]
    assert edited and edited[-1]["details"]["entity"] == "agent:daily_briefing_agent"


def test_team_file_traversal_refused(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path, examples=True)
    rc = main(["--home", str(tmp_path), "team", "file", "set", "marketing_team",
               "../../agents/copywriter.yaml", "--content", "pwned"])
    assert rc == 1
    assert "escapes the workspace" in capsys.readouterr().out
    assert "pwned" not in (tmp_path / "agents" / "copywriter.yaml").read_text(encoding="utf-8")


def test_team_files_and_plan_edit(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path, examples=True)
    assert main(["--home", str(tmp_path), "team", "files", "marketing_team", "--json"]) == 0
    names = {f["name"] for f in json.loads(capsys.readouterr().out)}
    assert {"TEAM.md", "notes/plan.md", "shared-context/priorities.md"} <= names

    assert main(["--home", str(tmp_path), "team", "file", "set", "marketing_team",
                 "notes/plan.md", "--content", "# Plan\nShip aurora."]) == 0
    capsys.readouterr()
    assert (tmp_path / "workspaces" / "marketing_team" / "notes" / "plan.md").read_text(
        encoding="utf-8") == "# Plan\nShip aurora."


def test_recipes_cat_and_save_with_validation_rollback(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "recipes", "cat", "researcher"]) == 0
    original = capsys.readouterr().out
    assert original.startswith("---") and "kind: agent" in original

    # save a user copy (overrides bundled — find_recipe prefers the user dir)
    edited = original.replace("Gathers and summarizes", "Hunts down and verifies")
    assert main(["--home", str(tmp_path), "recipes", "save", "researcher",
                 "--content", edited, "--json"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["path"].endswith("recipes/researcher.md")
    assert main(["--home", str(tmp_path), "recipes", "cat", "researcher"]) == 0
    assert "Hunts down" in capsys.readouterr().out                     # user copy wins

    # invalid frontmatter → rolled back to the previous user copy
    rc = main(["--home", str(tmp_path), "recipes", "save", "researcher",
               "--content", "---\nname-only: true\n---\nbroken"])
    assert rc == 1
    assert "invalid recipe" in capsys.readouterr().out
    assert main(["--home", str(tmp_path), "recipes", "cat", "researcher"]) == 0
    assert "Hunts down" in capsys.readouterr().out                     # prior save intact

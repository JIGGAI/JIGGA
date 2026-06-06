"""Scaffold SYNC semantics (RJ 2026-06-06): "never touches YOUR files", not
"never touches existing files". Per artifact:

    missing                        → written
    exists, content identical      → unchanged (not rewritten)
    exists, PRISTINE (hash match)  → updated from the recipe (lossless)
    exists, edited or untracked    → skipped (notice; --overwrite / update
                                     picker are the explicit opt-ins)
"""

from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_workflows
from jigga.core.io import read_yaml
from jigga.runtime.recipes import find_recipe, installed_recipes, load_recipe, scaffold_team


def _install(paths):
    recipe = load_recipe(find_recipe(paths.home, "marketing-team"))
    return recipe, scaffold_team(paths.home, recipe, agents_dir=paths.agents,
                                 teams_dir=paths.teams, workflows_dir=paths.workflows)


def _evolve_recipe(paths) -> None:
    """Simulate a shipped-recipe change: copywriter's role text evolves."""
    source = find_recipe(paths.home, "marketing-team")
    user_copy = paths.home / "recipes" / "marketing-team.md"
    user_copy.parent.mkdir(parents=True, exist_ok=True)
    user_copy.write_text(source.read_text(encoding="utf-8").replace(
        "Writes punchy launch copy", "Writes electrifying launch copy"), encoding="utf-8")
    # repoint the record so the next scaffold reads the evolved copy
    record_path = paths.home / "state" / "recipes" / "marketing_team.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["source"] = str(user_copy)
    record_path.write_text(json.dumps(record), encoding="utf-8")


def test_missing_files_are_written(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _, summary = _install(paths)
    assert set(summary["agents_written"]) == {"marketing_lead", "copywriter", "seo_editor"}
    assert summary["agents_updated"] == [] and summary["agents_skipped"] == []
    assert summary["team_written"] is True
    assert summary["workflows_written"] == ["team_launch"]


def test_unchanged_files_are_not_rewritten(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    recipe, _ = _install(paths)
    target = paths.agents / "copywriter.yaml"
    mtime = target.stat().st_mtime_ns
    summary = scaffold_team(paths.home, recipe, agents_dir=paths.agents,
                            teams_dir=paths.teams, workflows_dir=paths.workflows)
    assert summary["agents_written"] == [] and summary["agents_updated"] == []
    assert summary["agents_skipped"] == []                        # unchanged ≠ skipped-edited
    assert target.stat().st_mtime_ns == mtime                     # genuinely untouched


def test_pristine_files_update_when_recipe_evolves(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _install(paths)
    _evolve_recipe(paths)

    user_recipe = load_recipe(paths.home / "recipes" / "marketing-team.md")
    summary = scaffold_team(paths.home, user_recipe, agents_dir=paths.agents,
                            teams_dir=paths.teams, workflows_dir=paths.workflows)
    assert summary["agents_updated"] == ["copywriter"]            # pristine → synced
    assert "electrifying" in load_agents(paths.agents)["copywriter"].role
    # record hash refreshed → still pristine afterwards, and idempotent
    record = next(r for r in installed_recipes(paths.home) if r["scaffold_id"] == "marketing_team")
    assert record["modified"] == []
    again = scaffold_team(paths.home, user_recipe, agents_dir=paths.agents,
                          teams_dir=paths.teams, workflows_dir=paths.workflows)
    assert again["agents_updated"] == [] and again["agents_skipped"] == []


def test_edited_files_are_never_synced(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _install(paths)
    target = paths.agents / "copywriter.yaml"
    target.write_text(target.read_text(encoding="utf-8") + "# my tweak\n", encoding="utf-8")
    _evolve_recipe(paths)

    user_recipe = load_recipe(paths.home / "recipes" / "marketing-team.md")
    summary = scaffold_team(paths.home, user_recipe, agents_dir=paths.agents,
                            teams_dir=paths.teams, workflows_dir=paths.workflows)
    assert summary["agents_skipped"] == ["copywriter"]
    text = target.read_text(encoding="utf-8")
    assert "# my tweak" in text and "electrifying" not in text    # edit sacred


def test_untracked_existing_files_treated_as_edited(tmp_path: Path) -> None:
    """Existing file with NO recorded hash (pre-record installs, or a hand-made
    agent colliding with a recipe id) must never be clobbered."""
    from jigga.core.io import write_yaml

    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "copywriter.yaml", {"id": "copywriter", "name": "MINE",
               "role": "precious", "memory_scope": "task_only", "tools": [], "permissions": {}})
    recipe = load_recipe(find_recipe(paths.home, "marketing-team"))
    summary = scaffold_team(paths.home, recipe, agents_dir=paths.agents,
                            teams_dir=paths.teams, workflows_dir=paths.workflows)
    assert "copywriter" in summary["agents_skipped"]
    assert load_agents(paths.agents)["copywriter"].name == "MINE"


def test_corrupt_install_record_degrades_to_safe_skip(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    recipe, _ = _install(paths)
    (paths.home / "state" / "recipes" / "marketing_team.json").write_text("{not json", encoding="utf-8")
    _evolve_recipe_safe = (paths.agents / "copywriter.yaml")
    before = _evolve_recipe_safe.read_text(encoding="utf-8")
    scaffold_team(paths.home, recipe, agents_dir=paths.agents,
                  teams_dir=paths.teams, workflows_dir=paths.workflows)  # must not raise
    assert _evolve_recipe_safe.read_text(encoding="utf-8") == before


def test_team_yaml_and_workflows_follow_the_same_policy(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _install(paths)
    # evolve the recipe's team purpose + workflow content
    source = find_recipe(paths.home, "marketing-team")
    user_copy = paths.home / "recipes" / "marketing-team.md"
    user_copy.parent.mkdir(parents=True, exist_ok=True)
    user_copy.write_text(source.read_text(encoding="utf-8")
                         .replace("Turn a product brief into reviewed launch copy.",
                                  "Ship the aurora launch.")
                         .replace("Give 3 short bullet notes", "Give 5 short bullet notes"),
                         encoding="utf-8")
    record_path = paths.home / "state" / "recipes" / "marketing_team.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["source"] = str(user_copy)
    record_path.write_text(json.dumps(record), encoding="utf-8")

    summary = scaffold_team(paths.home, load_recipe(user_copy), agents_dir=paths.agents,
                            teams_dir=paths.teams, workflows_dir=paths.workflows)
    assert summary["team_written"] is True                         # pristine team yaml synced
    assert read_yaml(paths.teams / "marketing_team.yaml")["purpose"] == "Ship the aurora launch."
    assert summary["workflows_updated"] == ["team_launch"]         # pristine workflow synced
    flow = load_workflows(paths.workflows)["team_launch"]
    assert "5 short bullet notes" in str(flow.steps[2].input)

    # but an EDITED workflow stays put
    wf = paths.workflows / "team_launch.yaml"
    wf.write_text(wf.read_text(encoding="utf-8") + "# tuned\n", encoding="utf-8")
    user_copy.write_text(user_copy.read_text(encoding="utf-8").replace("5 short", "7 short"),
                         encoding="utf-8")
    summary = scaffold_team(paths.home, load_recipe(user_copy), agents_dir=paths.agents,
                            teams_dir=paths.teams, workflows_dir=paths.workflows)
    assert summary["workflows_skipped"] == ["team_launch"]
    assert "# tuned" in wf.read_text(encoding="utf-8")


def test_cli_init_examples_self_heals_pristine_installs(tmp_path: Path, capsys) -> None:
    """The end-to-end payoff: init --examples twice across a 'version change'
    syncs pristine files without touching anything edited."""
    paths = init_runtime(tmp_path, examples=True)
    # user edits one agent
    target = paths.agents / "seo_editor.yaml"
    target.write_text(target.read_text(encoding="utf-8") + "# keep me\n", encoding="utf-8")
    # 'ship' an evolved recipe via the user-dir override + record repoint
    source = find_recipe(paths.home, "marketing-team")
    user_copy = paths.home / "recipes" / "marketing-team.md"
    user_copy.parent.mkdir(parents=True, exist_ok=True)
    user_copy.write_text(source.read_text(encoding="utf-8").replace(
        "Writes punchy launch copy", "Writes electrifying launch copy"), encoding="utf-8")
    record_path = paths.home / "state" / "recipes" / "marketing_team.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["source"] = str(user_copy)
    record_path.write_text(json.dumps(record), encoding="utf-8")

    assert main(["--home", str(tmp_path), "recipes", "scaffold", "marketing-team", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["agents_updated"] == ["copywriter"]
    assert summary["agents_skipped"] == ["seo_editor"] or "seo_editor" in summary["agents_skipped"]
    assert "# keep me" in target.read_text(encoding="utf-8")

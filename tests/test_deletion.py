"""agents delete / team delete — destructive, so: backed up, recipe-aware
(de-staff to membership-only), record-owned-only for teams, audited."""

from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.config import load_agents
from jigga.core.io import write_yaml
from jigga.runtime.recipes import load_recipe


def test_delete_agent_backs_up_and_destaffs_recipe(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path, examples=True)
    assert main(["--home", str(tmp_path), "agents", "delete", "copywriter", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert "copywriter" not in load_agents(paths.agents)
    # backed up before deletion
    assert any(b.endswith("agents/copywriter.yaml") for b in result["backups"])
    backup = next(b for b in result["backups"] if b.endswith("agents/copywriter.yaml"))
    assert "Copywriter" in Path(backup).read_text(encoding="utf-8")
    # recipe member de-staffed → membership-only again (source of truth intact)
    recipe = load_recipe(Path(result["destaffed_recipe"]))
    member = next(m for m in recipe.agents if m.get("id") == "copywriter")
    assert "agent" not in member
    # roster entry survives (workflows/handoffs may reference it)
    from jigga.core.config import load_teams
    team = load_teams(paths.teams)["marketing_team"]
    assert any(m.get("id") == "copywriter" for m in team.agents)
    # install record no longer tracks it
    from jigga.runtime.recipes import installed_recipes
    record = next(r for r in installed_recipes(paths.home) if r["scaffold_id"] == "marketing_team")
    assert "agents/copywriter.yaml" not in record["artifacts"]

    events = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "agent.deleted" for e in events)


def test_delete_team_removes_owned_artifacts_only(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path, examples=True)
    # a hand-written agent ON THE ROSTER (but not record-owned) must survive
    write_yaml(paths.agents / "outsider.yaml", {"id": "outsider", "name": "O", "role": "r",
               "memory_scope": "task_only", "tools": [], "permissions": {}})
    from jigga.core.io import read_yaml
    team_doc = read_yaml(paths.teams / "marketing_team.yaml")
    team_doc["agents"].append({"id": "outsider", "role": "extra", "required": False})
    write_yaml(paths.teams / "marketing_team.yaml", team_doc)

    assert main(["--home", str(tmp_path), "team", "delete", "marketing_team", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert not (paths.teams / "marketing_team.yaml").exists()
    assert not (tmp_path / "workspaces" / "marketing_team").exists()
    agents = load_agents(paths.agents)
    assert "copywriter" not in agents and "marketing_lead" not in agents   # record-owned
    assert "outsider" in agents                                            # untouched
    assert "daily_briefing_agent" in agents                                # other team untouched
    assert not (paths.workflows / "team_launch.yaml").exists()             # record-owned workflow
    assert (paths.workflows / "morning_day_summary.yaml").exists()         # other team's workflow
    # everything backed up
    assert any("teams/marketing_team.yaml" in b for b in result["backups"])
    assert any("workspaces/marketing_team" in b for b in result["backups"])
    # record gone → recipes list shows uninstalled
    from jigga.runtime.recipes import installed_recipes
    assert not any(r["scaffold_id"] == "marketing_team" for r in installed_recipes(paths.home))


def test_delete_solo_agent_drops_its_record_and_workspace(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "recipes", "scaffold", "researcher"]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "agents", "delete", "researcher"]) == 0
    capsys.readouterr()
    assert "researcher" not in load_agents(paths.agents)
    assert not (tmp_path / "workspaces" / "researcher").exists()
    from jigga.runtime.recipes import installed_recipes
    assert not any(r["scaffold_id"] == "researcher" for r in installed_recipes(paths.home))


def test_delete_missing_entities_fail_cleanly(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "agents", "delete", "nope"]) == 1
    assert "No such agent" in capsys.readouterr().out
    assert main(["--home", str(tmp_path), "team", "delete", "nope"]) == 1
    assert "No such team" in capsys.readouterr().out

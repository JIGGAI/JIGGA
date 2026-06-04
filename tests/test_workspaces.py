from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.models import TeamConfig
from jigga.runtime.workspaces import (
    CuratorError,
    append_agent_output,
    append_status,
    is_curated,
    read_file,
    scaffold_workspace,
    team_lead,
    workspace_dir,
    write_curated,
)


def _team() -> TeamConfig:
    return TeamConfig(
        id="marketing_team", name="Marketing Team", purpose="Launch copy.",
        agents=[{"id": "marketing_lead", "role": "strategy", "required": True},
                {"id": "copywriter", "role": "drafting", "required": True},
                {"id": "seo_editor", "role": "review", "required": True}],
        routing={"default_assignee": "marketing_lead"},
    )


def test_team_lead_prefers_default_assignee() -> None:
    assert team_lead(_team()) == "marketing_lead"
    assert team_lead(TeamConfig(id="t", name="T", agents=[{"id": "a"}, {"id": "b"}])) == "a"


def test_scaffold_creates_layout_and_is_idempotent(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    summary = scaffold_workspace(paths.home, _team())
    root = workspace_dir(paths.home, "marketing_team")
    assert summary["lead"] == "marketing_lead"
    for rel in ("TEAM.md", "notes/plan.md", "notes/status.md", "shared-context/priorities.md"):
        assert (root / rel).exists()
    for member in ("marketing_lead", "copywriter", "seo_editor"):
        assert (root / "roles" / member).is_dir()
    assert (root / "shared-context" / "agent-outputs").is_dir()
    assert (root / "shared-context" / "feedback").is_dir()

    # idempotent: re-run doesn't overwrite or re-report curated files
    (root / "notes" / "plan.md").write_text("EDITED BY LEAD", encoding="utf-8")
    again = scaffold_workspace(paths.home, _team())
    assert again["created"] == []
    assert read_file(paths.home, "marketing_team", "notes/plan.md") == "EDITED BY LEAD"


def test_curator_model(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    team = _team()
    scaffold_workspace(paths.home, team)
    assert is_curated("notes/plan.md") and is_curated("shared-context/priorities.md")
    assert not is_curated("notes/status.md")

    # lead may write curated files
    write_curated(paths.home, team, "notes/plan.md", "# Q3 plan", member="marketing_lead")
    assert read_file(paths.home, "marketing_team", "notes/plan.md") == "# Q3 plan"

    # a non-lead may not
    with pytest.raises(CuratorError):
        write_curated(paths.home, team, "shared-context/priorities.md", "mine", member="copywriter")

    # non-curated file rejected by write_curated
    with pytest.raises(ValueError):
        write_curated(paths.home, team, "notes/status.md", "x", member="marketing_lead")


def test_append_helpers(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    scaffold_workspace(paths.home, _team())
    append_status(paths.home, "marketing_team", "drafted the launch tweet")
    append_agent_output(paths.home, "marketing_team", "copywriter", "Tweet v1: ...")
    assert "drafted the launch tweet" in read_file(paths.home, "marketing_team", "notes/status.md")
    out = read_file(paths.home, "marketing_team", "shared-context/agent-outputs/copywriter.md")
    assert "Tweet v1" in out


# --- CLI -------------------------------------------------------------------


def test_cli_team_init_and_workspace(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path, examples=True)  # scaffolds marketing_team (recipe) incl. its workspace
    assert main(["--home", str(tmp_path), "team", "init", "marketing_team", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["lead"] == "marketing_lead"
    # The recipe scaffold already created the workspace at init; team init is
    # idempotent and reports nothing new.
    assert summary["created"] == []
    assert (Path(summary["workspace"]) / "notes" / "plan.md").exists()

    assert main(["--home", str(tmp_path), "team", "workspace", "marketing_team"]) == 0
    listing = capsys.readouterr().out
    assert "TEAM.md" in listing and "shared-context/priorities.md" in listing


def test_scaffold_gives_every_member_soul_and_memory_starters(tmp_path: Path) -> None:
    """Every agent has SOUL + MEMORY at minimum (AGENTS/TOOLS layers are
    generated live so they can't go stale). Flows to recipe scaffolds, team
    init, and solo agents — they all come through scaffold_workspace."""
    from jigga.commands.init import init_runtime
    from jigga.core.config import load_teams
    from jigga.runtime.workspaces import read_file, scaffold_workspace

    paths = init_runtime(tmp_path, examples=True)  # scaffolds the example team recipes
    for member in ("marketing_lead", "copywriter", "seo_editor"):
        memory = read_file(paths.home, "marketing_team", f"roles/{member}/MEMORY.md")
        assert memory and "Curate your durable notes" in memory
        assert read_file(paths.home, "marketing_team", f"roles/{member}/SOUL.md")

    # create-only: a member's curated memory survives re-scaffold
    target = (tmp_path / "workspaces" / "marketing_team" / "roles" / "copywriter" / "MEMORY.md")
    target.write_text("MINE", encoding="utf-8")
    scaffold_workspace(paths.home, load_teams(paths.teams)["marketing_team"])
    assert target.read_text(encoding="utf-8") == "MINE"


def test_solo_agent_recipe_scaffold_gets_memory_starter(tmp_path: Path) -> None:
    from jigga.commands.init import init_runtime
    from jigga.runtime.recipes import find_recipe, load_recipe, scaffold_agent
    from jigga.runtime.workspaces import read_file

    paths = init_runtime(tmp_path)
    recipe = load_recipe(find_recipe(paths.home, "researcher"))
    scaffold_agent(paths.home, recipe, agents_dir=paths.agents)
    memory = read_file(paths.home, "researcher", "roles/researcher/MEMORY.md")
    assert memory and "Curate your durable notes" in memory

"""An agent's own file says which team it works for.

Membership lived only in the team roster, so `jigga agents get
content_strategist` answered `team: null` for an agent plainly on
social_content_team — you had to scan every team yaml to find out. The field is
DESCRIPTIVE: `find_agent_teams` still decides membership, nothing about
scheduling or workspace resolution reads it, and `jigga validate` warns when
the two disagree rather than letting a wrong value misroute work.

Also here: AGENTS.md and TOOLS.md stop being reported as required-and-missing.
Scaffolding deliberately never writes them — they are rendered per run from the
live roster and grants — so the manifest was flagging a runtime that was
working exactly as designed.
"""

from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_teams
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.entity_files import list_agent_files
from jigga.runtime.validation import validate_configs


def _scaffold_team(tmp_path: Path, recipe: str = "social-content-team", team_id: str | None = None):
    paths = init_runtime(tmp_path)
    args = ["--home", str(tmp_path), "recipes", "scaffold", recipe]
    if team_id:
        args += ["--id", team_id]
    assert main(args) == 0
    return paths


# --- the field is written where an agent is installed ------------------------


def test_a_team_agent_records_its_team(tmp_path: Path, capsys) -> None:
    _scaffold_team(tmp_path)
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "agents", "get", "content_strategist", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["team"] == "social_content_team"


def test_the_team_it_landed_on_wins_over_the_recipe_default(tmp_path: Path) -> None:
    # The same recipe scaffolds under --id; a team frozen into the recipe would
    # name a team the agent is not on.
    _scaffold_team(tmp_path, team_id="other_squad")
    agents = load_agents(tmp_path / "agents")
    assert all(a.team == "other_squad" for a in agents.values())


def test_a_solo_agent_has_no_team(tmp_path: Path) -> None:
    # A team-less agent gets a workspace of its own named after it — that is a
    # workspace, not a team, and claiming one would be a lie the UI repeats.
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "recipes", "scaffold", "researcher"]) == 0
    assert load_agents(tmp_path / "agents")["researcher"].team is None


def test_staffing_a_member_later_records_the_team_too(tmp_path: Path) -> None:
    # `team staff` re-scaffolds through the same writer, so a member staffed
    # after the team was created is not a second code path to keep in sync.
    _scaffold_team(tmp_path)
    assert main(["--home", str(tmp_path), "team", "staff", "social_content_team",
                 "late_joiner", "--role", "late addition"]) == 0
    assert load_agents(tmp_path / "agents")["late_joiner"].team == "social_content_team"


# --- descriptive, not authoritative ------------------------------------------


def test_a_disagreement_is_a_warning_not_an_error(tmp_path: Path) -> None:
    _scaffold_team(tmp_path)
    agents_dir = tmp_path / "agents"
    doc = read_yaml(agents_dir / "content_strategist.yaml")
    doc["team"] = "some_other_team"
    write_yaml(agents_dir / "content_strategist.yaml", doc)

    problems = validate_configs(load_agents(agents_dir), load_teams(tmp_path / "teams"))
    mismatch = [p for p in problems if "content_strategist" in p and "some_other_team" in p]
    assert mismatch and mismatch[0].startswith("warning:")


def test_membership_still_comes_from_the_roster(tmp_path: Path, capsys) -> None:
    # The whole point of descriptive: a wrong field must not change the answer.
    _scaffold_team(tmp_path)
    doc = read_yaml(tmp_path / "agents" / "content_strategist.yaml")
    doc["team"] = "some_other_team"
    write_yaml(tmp_path / "agents" / "content_strategist.yaml", doc)

    capsys.readouterr()
    assert main(["--home", str(tmp_path), "agents", "list", "--json"]) == 0
    listed = {a["id"]: a["team"] for a in json.loads(capsys.readouterr().out)}
    assert listed["content_strategist"] == "social_content_team"


def test_an_agent_on_no_roster_declaring_a_team_is_flagged(tmp_path: Path) -> None:
    _scaffold_team(tmp_path)
    write_yaml(tmp_path / "agents" / "stranger.yaml",
               {"id": "stranger", "name": "Stranger", "role": "x", "team": "ghost_team"})
    problems = validate_configs(load_agents(tmp_path / "agents"), load_teams(tmp_path / "teams"))
    assert any("stranger" in p and "does not exist" in p for p in problems)


# --- existing installs pick it up --------------------------------------------


def test_an_agent_installed_before_this_change_gets_it_on_update(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    """`jigga update` regenerates recipe artifacts and three-way compares, so a
    pristine agent yaml gains the field with no migration code."""
    import subprocess

    from jigga.runtime import service
    from jigga.runtime.recipes import _records_dir, _sha256

    # `update` refreshes the supervisor service; answer for the OS rather than
    # letting the suite drive this machine's real systemd (#205).
    monkeypatch.setattr(service, "_default_run", lambda argv: subprocess.CompletedProcess(
        args=argv, returncode=1, stdout="inactive\n", stderr=""))

    _scaffold_team(tmp_path)
    agent_path = tmp_path / "agents" / "content_strategist.yaml"

    # Rewind to a pre-change install: no `team:`, and the install record hashed
    # to THAT content, which is what "pristine" means to the reconciler.
    doc = read_yaml(agent_path)
    doc.pop("team")
    write_yaml(agent_path, doc)
    record_path = _records_dir(tmp_path) / "social_content_team.json"
    record = json.loads(record_path.read_text())
    record["hashes"]["agents/content_strategist.yaml"] = _sha256(agent_path)
    record_path.write_text(json.dumps(record))
    assert read_yaml(agent_path).get("team") is None

    capsys.readouterr()
    assert main(["--home", str(tmp_path), "update", "--apply", "--json"]) == 0
    assert read_yaml(agent_path)["team"] == "social_content_team"


# --- generated files are not missing files -----------------------------------


def test_agents_md_is_generated_not_required(tmp_path: Path) -> None:
    # Scaffolding never writes it; the context pack renders it from the live
    # roster. Reporting it "missing and required" flagged every team agent.
    _scaffold_team(tmp_path)
    listing = {f["name"]: f for f in list_agent_files(tmp_path, "social_content_team", "content_strategist")}
    assert listing["AGENTS.md"] == {"name": "AGENTS.md", "required": False,
                                    "generated": True, "missing": True}
    assert listing["TOOLS.md"]["generated"] is True


def test_the_files_scaffolding_does_write_stay_required(tmp_path: Path) -> None:
    _scaffold_team(tmp_path)
    listing = {f["name"]: f for f in list_agent_files(tmp_path, "social_content_team", "content_strategist")}
    for name in ("SOUL.md", "MEMORY.md"):
        assert listing[name]["required"] is True
        assert listing[name]["generated"] is False
        assert listing[name]["missing"] is False, f"{name} should be scaffolded for every member"

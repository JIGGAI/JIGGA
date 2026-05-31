from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_teams
from jigga.runtime.recipes import find_recipe, list_recipes, load_recipe, scaffold_agent, scaffold_team

RECIPE = """---
id: marketing-team
name: Marketing Team
kind: team
purpose: Turn a brief into launch copy.
routing:
  lead: lead
agents:
  - role: lead
    name: "{{teamName}} Lead"
    description: Distills the product for {{teamId}}.
    tools: [draft_with_model]
  - role: copywriter
    name: Copywriter
    tools: [draft_with_model]
---

# body ignored by the scaffolder
"""


def _write_recipe(tmp_path: Path) -> Path:
    path = tmp_path / "mt.md"
    path.write_text(RECIPE, encoding="utf-8")
    return path


def test_load_recipe_parses_frontmatter(tmp_path: Path) -> None:
    recipe = load_recipe(_write_recipe(tmp_path))
    assert recipe.id == "marketing-team" and recipe.kind == "team"
    assert [a["role"] for a in recipe.agents] == ["lead", "copywriter"]
    assert recipe.routing["lead"] == "lead"


def test_scaffold_team_writes_agents_team_workspace_with_templating(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    recipe = load_recipe(_write_recipe(tmp_path))
    summary = scaffold_team(paths.home, recipe, team_id="mm", agents_dir=paths.agents, teams_dir=paths.teams)

    assert summary["lead"] == "mm-lead"
    assert set(summary["agents_written"]) == {"mm-lead", "mm-copywriter"}

    agents = load_agents(paths.agents)
    assert "mm-lead" in agents and "mm-copywriter" in agents
    assert agents["mm-lead"].name == "Marketing Team Lead"          # {{teamName}} templated
    assert "mm" in agents["mm-lead"].role                            # {{teamId}} templated

    team = load_teams(paths.teams)["mm"]
    assert team.routing["default_assignee"] == "mm-lead"
    assert {m["id"] for m in team.agents} == {"mm-lead", "mm-copywriter"}

    # workspace scaffolded
    assert (Path(summary["workspace"]) / "notes" / "plan.md").exists()


def test_scaffold_is_create_only_unless_overwrite(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    recipe = load_recipe(_write_recipe(tmp_path))
    scaffold_team(paths.home, recipe, team_id="mm", agents_dir=paths.agents, teams_dir=paths.teams)
    (paths.agents / "mm-lead.yaml").write_text("id: mm-lead\nname: EDITED\nrole: x\n", encoding="utf-8")

    again = scaffold_team(paths.home, recipe, team_id="mm", agents_dir=paths.agents, teams_dir=paths.teams)
    assert "mm-lead" in again["agents_skipped"]                     # not overwritten
    assert load_agents(paths.agents)["mm-lead"].name == "EDITED"

    forced = scaffold_team(paths.home, recipe, team_id="mm", overwrite=True,
                           agents_dir=paths.agents, teams_dir=paths.teams)
    assert "mm-lead" in forced["agents_written"]
    assert load_agents(paths.agents)["mm-lead"].name == "Marketing Team Lead"


def test_bundled_recipe_resolves_and_scaffolds_via_cli(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    # bundled examples/recipes/marketing-team.md is discoverable by name
    assert find_recipe(tmp_path, "marketing-team") is not None
    assert any(r["id"] == "marketing-team" for r in list_recipes(tmp_path))

    assert main(["--home", str(tmp_path), "team", "scaffold", "marketing-team",
                 "--team-id", "acme-mktg", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["lead"] == "acme-mktg-lead"
    assert "acme-mktg-editor" in summary["agents_written"]
    # the scaffolded team + agents are loadable
    assert "acme-mktg" in load_teams((tmp_path / "teams"))
    assert "acme-mktg-copywriter" in load_agents((tmp_path / "agents"))


def test_scaffold_team_rejects_agent_kind(tmp_path: Path) -> None:
    path = tmp_path / "solo.md"
    path.write_text("---\nid: solo\nname: Solo\nkind: agent\n---\n", encoding="utf-8")
    with pytest.raises(ValueError):
        scaffold_team(tmp_path, load_recipe(path))


# --- W4 follow-ups: cronJobs + kind: agent ---------------------------------

CRON_RECIPE = """---
id: ops-team
name: Ops Team
kind: team
routing: {lead: lead}
agents:
  - role: lead
    name: Lead
    tools: [draft_with_model]
    cronJobs:
      - id: triage
        schedule: "*/30 7-23 * * 1-5"
        enabledByDefault: true
        message: "Triage {{teamId}} work."
      - id: nightly
        schedule: "0 2 * * *"
        enabledByDefault: false
        message: "off by default"
---
"""

AGENT_RECIPE = """---
id: researcher
name: Researcher
kind: agent
model: profile:default
tools: [summarize_relevant_context]
cronJobs:
  - id: morning
    schedule: "0 8 * * 1-5"
    enabledByDefault: true
    message: "Do the morning briefing."
---
"""


def test_cronjobs_become_wake_schedules(tmp_path: Path) -> None:
    from jigga.core.config import load_agents
    paths = init_runtime(tmp_path)
    recipe_path = tmp_path / "ops.md"
    recipe_path.write_text(CRON_RECIPE, encoding="utf-8")
    scaffold_team(paths.home, load_recipe(recipe_path), team_id="ops",
                  agents_dir=paths.agents, teams_dir=paths.teams)
    lead = load_agents(paths.agents)["ops-lead"]
    schedules = lead.wake.get("schedules", [])
    assert len(schedules) == 1                                  # disabled one skipped (safe-idle)
    assert schedules[0]["cron"] == "*/30 7-23 * * 1-5"
    assert schedules[0]["message"] == "Triage ops work."        # {{teamId}} templated


def test_scaffold_agent_kind(tmp_path: Path) -> None:
    from jigga.core.config import load_agents
    paths = init_runtime(tmp_path)
    recipe_path = tmp_path / "r.md"
    recipe_path.write_text(AGENT_RECIPE, encoding="utf-8")
    summary = scaffold_agent(paths.home, load_recipe(recipe_path), agent_id="rsx", agents_dir=paths.agents)
    assert summary["kind"] == "agent" and summary["scheduled"] is True
    agent = load_agents(paths.agents)["rsx"]
    assert agent.tools == ["summarize_relevant_context"]
    assert agent.wake["schedules"][0]["message"] == "Do the morning briefing."


def test_cli_scaffold_kind_agent(tmp_path: Path, capsys) -> None:
    from jigga.core.config import load_agents
    init_runtime(tmp_path)  # bundled researcher.md recipe
    assert main(["--home", str(tmp_path), "team", "scaffold", "researcher", "--team-id", "rr", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["kind"] == "agent" and summary["agent_id"] == "rr"
    assert "rr" in load_agents((tmp_path / "agents"))


def test_scheduled_workloop_message_becomes_task_description(tmp_path: Path) -> None:
    """End-to-end: a cron-due agent → supervisor creates a task whose body is the
    cronJob message."""
    from unittest.mock import patch
    from jigga.core.io import write_yaml
    from jigga.runtime.model_router import ModelCallResult
    from jigga.runtime.supervisor import supervisor_tick
    from jigga.runtime.tasks import list_tasks

    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "looper.yaml", {
        "id": "looper", "name": "Looper", "role": "x", "memory_scope": "task_only", "tools": [],
        "permissions": {}, "wake": {"schedules": [{"cron": "* * * * *", "event": "loop",
                                    "message": "do the loop work"}]}})

    def _noop_model(home, logs_dir, request):  # cron "* * * * *" is always due
        return ModelCallResult(status="ok", provider="dry_run", model="m", content="ok", dry_run=True, tool_calls=[])

    with patch("jigga.runtime.agent.call_model", _noop_model):
        supervisor_tick(paths.home)
    looper_tasks = [t for t in list_tasks(paths.tasks) if t.assignee == "looper"]
    assert looper_tasks and looper_tasks[0].description == "do the loop work"

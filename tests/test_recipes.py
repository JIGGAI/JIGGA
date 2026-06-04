from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_teams
from jigga.runtime.recipes import (
    find_recipe,
    list_recipes,
    load_recipe,
    recipe_summary,
    scaffold_agent,
    scaffold_team,
)

RECIPE = """---
id: marketing-team
name: Marketing Team
kind: team
purpose: Turn a brief into launch copy.
routing:
  lead: lead
agents:
  - role: lead
    agent:
      name: "{{teamName}} Lead"
      role: Distills the product for {{teamId}}.
      tools: [draft_with_model]
  - role: copywriter
    agent:
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
    assert any(r["id"] == "marketing_team" for r in list_recipes(tmp_path))

    assert main(["--home", str(tmp_path), "recipes", "scaffold", "marketing-team",
                 "--id", "acme-mktg", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    # bundled members pin explicit agent ids; --id only overrides the team id
    assert summary["lead"] == "marketing_lead"
    assert "seo_editor" in summary["agents_written"]
    assert "team_launch" in summary["workflows_written"]
    assert "acme-mktg" in load_teams((tmp_path / "teams"))
    assert "copywriter" in load_agents((tmp_path / "agents"))


def test_scaffold_team_rejects_agent_kind(tmp_path: Path) -> None:
    path = tmp_path / "solo.md"
    path.write_text("---\nid: solo\nname: Solo\nkind: agent\nagent: {role: x}\n---\n", encoding="utf-8")
    with pytest.raises(ValueError):
        scaffold_team(tmp_path, load_recipe(path))


def test_scaffold_agent_requires_agent_map(tmp_path: Path) -> None:
    path = tmp_path / "bare.md"
    path.write_text("---\nid: bare\nname: Bare\nkind: agent\n---\n", encoding="utf-8")
    with pytest.raises(ValueError, match="agent"):
        scaffold_agent(tmp_path, load_recipe(path))


# --- cronJobs + kind: agent --------------------------------------------------

CRON_RECIPE = """---
id: ops-team
name: Ops Team
kind: team
routing: {lead: lead}
agents:
  - role: lead
    agent:
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
agent:
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
    paths = init_runtime(tmp_path)
    recipe_path = tmp_path / "r.md"
    recipe_path.write_text(AGENT_RECIPE, encoding="utf-8")
    summary = scaffold_agent(paths.home, load_recipe(recipe_path), agent_id="rsx", agents_dir=paths.agents)
    assert summary["kind"] == "agent" and summary["scheduled"] is True
    agent = load_agents(paths.agents)["rsx"]
    assert agent.tools == ["summarize_relevant_context"]
    assert agent.wake["schedules"][0]["message"] == "Do the morning briefing."


def test_cli_scaffold_kind_agent(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)  # bundled researcher.md recipe
    assert main(["--home", str(tmp_path), "recipes", "scaffold", "researcher", "--id", "rr", "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["kind"] == "agent" and summary["agent_id"] == "rr"
    assert "rr" in load_agents((tmp_path / "agents"))


# --- full-fidelity member specs ----------------------------------------------

FIDELITY_RECIPE = """---
id: admin-team
name: Admin Team
kind: team
memory_scope: manager_view
routing:
  lead: briefing
  handoffs:
    - from: briefer
      to: prepper
      when: briefing_done
default_workflows:
  - daily_flow
policies:
  approvals:
    required_for: [send_email]
agents:
  - role: briefing
    id: briefer
    required: true
    agent:
      name: Briefer
      role: Briefs the user.
      memory_scope: manager_view
      tools: [notifications.send]
      notifications:
        channel: default
      wake:
        events: [task.assigned.briefer]
        accepts_agent_requests: true
      delegation:
        enabled: true
        allowed_backends: [dry_run]
      permissions:
        notifications: send
        network: {mode: deny}
        shell: {mode: deny}
  - role: meeting prep
    id: prepper
    required: false
workflows:
  - id: daily_flow
    name: Daily Flow
    status: approved
    trigger: {manual: true}
    steps:
      - id: notify
        agent: briefer
        action: notifications.send
        input: {content: "hello {{teamId}}"}
---
"""


def test_member_agent_map_passes_through_full_agent_yaml(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    recipe_path = tmp_path / "admin.md"
    recipe_path.write_text(FIDELITY_RECIPE, encoding="utf-8")
    summary = scaffold_team(paths.home, load_recipe(recipe_path),
                            agents_dir=paths.agents, teams_dir=paths.teams,
                            workflows_dir=paths.workflows)

    # Explicit member id wins over <teamId>-<role>
    assert summary["agents_written"] == ["briefer"]
    briefer = load_agents(paths.agents)["briefer"]
    # Full passthrough: every AgentConfig field from the recipe lands in the yaml
    assert briefer.notifications == {"channel": "default"}
    assert briefer.memory_scope == "manager_view"
    assert briefer.wake["events"] == ["task.assigned.briefer"]
    assert briefer.wake["accepts_agent_requests"] is True
    assert briefer.delegation["enabled"] is True
    assert briefer.permissions["notifications"] == "send"


def test_membership_only_members_listed_but_not_scaffolded(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    recipe_path = tmp_path / "admin.md"
    recipe_path.write_text(FIDELITY_RECIPE, encoding="utf-8")
    summary = scaffold_team(paths.home, load_recipe(recipe_path),
                            agents_dir=paths.agents, teams_dir=paths.teams,
                            workflows_dir=paths.workflows)

    assert "prepper" not in summary["agents_written"]
    assert not (paths.agents / "prepper.yaml").exists()              # no yaml generated
    team = load_teams(paths.teams)["admin-team"]
    prepper = next(m for m in team.agents if m["id"] == "prepper")   # ...but on the roster
    assert prepper["required"] is False


def test_team_yaml_passthrough_and_lead_resolution(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    recipe_path = tmp_path / "admin.md"
    recipe_path.write_text(FIDELITY_RECIPE, encoding="utf-8")
    scaffold_team(paths.home, load_recipe(recipe_path),
                  agents_dir=paths.agents, teams_dir=paths.teams, workflows_dir=paths.workflows)

    team = load_teams(paths.teams)["admin-team"]
    assert team.memory_scope == "manager_view"
    assert team.default_workflows == ["daily_flow"]
    assert team.policies["approvals"]["required_for"] == ["send_email"]
    # routing.lead names the ROLE; resolved to that member's explicit id
    assert team.routing["default_assignee"] == "briefer"
    assert team.routing["handoffs"][0]["to"] == "prepper"


def test_embedded_workflows_written_create_only(tmp_path: Path) -> None:
    from jigga.core.config import load_workflows

    paths = init_runtime(tmp_path)
    recipe_path = tmp_path / "admin.md"
    recipe_path.write_text(FIDELITY_RECIPE, encoding="utf-8")
    summary = scaffold_team(paths.home, load_recipe(recipe_path),
                            agents_dir=paths.agents, teams_dir=paths.teams,
                            workflows_dir=paths.workflows)

    assert summary["workflows_written"] == ["daily_flow"]
    flow = load_workflows(paths.workflows)["daily_flow"]
    assert flow.steps[0].input["content"] == "hello admin-team"      # {{teamId}} templated

    # create-only: a hand-edit survives re-scaffold
    (paths.workflows / "daily_flow.yaml").write_text(
        "id: daily_flow\nname: EDITED\nsteps: []\n", encoding="utf-8")
    again = scaffold_team(paths.home, load_recipe(recipe_path),
                          agents_dir=paths.agents, teams_dir=paths.teams,
                          workflows_dir=paths.workflows)
    assert again["workflows_written"] == [] and again["workflows_skipped"] == ["daily_flow"]
    assert load_workflows(paths.workflows)["daily_flow"].name == "EDITED"


# --- recipes CLI ---------------------------------------------------------------


def test_cli_recipes_list_and_show(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "recipes", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    ids = {r["id"] for r in listed}
    assert {"marketing_team", "personal_admin_team", "social_content_team", "researcher"} <= ids

    assert main(["--home", str(tmp_path), "recipes", "show", "personal-admin-team", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == "personal_admin_team" and shown["kind"] == "team"
    briefing = next(a for a in shown["agents"] if a["id"] == "daily_briefing_agent")
    assert briefing["scaffolded"] is True
    prep = next(a for a in shown["agents"] if a["id"] == "meeting_prep_agent")
    assert prep["scaffolded"] is False and prep["required"] is False
    assert "morning_day_summary" in shown["workflows"]


def test_cli_recipes_show_unknown_recipe_fails(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "recipes", "show", "nope"]) == 1
    assert "not found" in capsys.readouterr().out.lower()


def test_recipe_summary_shapes() -> None:
    bundled = Path(__file__).resolve().parents[1] / "examples" / "recipes"
    team = recipe_summary(load_recipe(bundled / "social-content-team.md"))
    assert team["kind"] == "team"
    assert any(a["id"] == "linkedin_writer" and not a["scaffolded"] for a in team["agents"])
    solo = recipe_summary(load_recipe(bundled / "researcher.md"))
    assert solo["kind"] == "agent" and solo["agents"][0]["scaffolded"] is True


# --- files / templates --------------------------------------------------------

FILES_RECIPE = """---
id: docs-team
name: Docs Team
kind: team
routing: {lead: lead}
templates:
  charter: "# {{teamName}} charter for {{teamId}}"
files:
  - path: notes/charter.md
    template: charter
    mode: createOnly
  - path: shared-context/conventions.md
    content: "Inline content, {{teamId}}."
  - path: "../escape.md"            # path traversal → must be refused
    content: "nope"
agents:
  - role: lead
    agent:
      name: Lead
      tools: [draft_with_model]
---
"""


def test_recipe_files_written_with_templating_and_traversal_guard(tmp_path: Path) -> None:
    from jigga.runtime.workspaces import read_file, workspace_dir
    paths = init_runtime(tmp_path)
    recipe_path = tmp_path / "docs.md"
    recipe_path.write_text(FILES_RECIPE, encoding="utf-8")
    summary = scaffold_team(paths.home, load_recipe(recipe_path), team_id="dx",
                            agents_dir=paths.agents, teams_dir=paths.teams)

    assert "notes/charter.md" in summary["files_written"]
    assert "shared-context/conventions.md" in summary["files_written"]
    assert "../escape.md" in summary["files_skipped"]                      # traversal refused
    assert not (workspace_dir(paths.home, "dx").parent / "escape.md").exists()

    assert read_file(paths.home, "dx", "notes/charter.md") == "# Docs Team charter for dx"  # templated
    assert read_file(paths.home, "dx", "shared-context/conventions.md") == "Inline content, dx."


def test_recipe_files_are_create_only(tmp_path: Path) -> None:
    from jigga.runtime.workspaces import read_file
    paths = init_runtime(tmp_path)
    recipe_path = tmp_path / "docs.md"
    recipe_path.write_text(FILES_RECIPE, encoding="utf-8")
    scaffold_team(paths.home, load_recipe(recipe_path), team_id="dx", agents_dir=paths.agents, teams_dir=paths.teams)
    # edit a recipe-written file, re-scaffold → not clobbered (createOnly)
    from jigga.runtime.workspaces import workspace_dir
    (workspace_dir(paths.home, "dx") / "notes" / "charter.md").write_text("HAND-EDITED", encoding="utf-8")
    again = scaffold_team(paths.home, load_recipe(recipe_path), team_id="dx", agents_dir=paths.agents, teams_dir=paths.teams)
    assert "notes/charter.md" in again["files_skipped"]
    assert read_file(paths.home, "dx", "notes/charter.md") == "HAND-EDITED"


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

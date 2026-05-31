from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.config import load_teams
from jigga.core.io import write_yaml
from jigga.runtime.agent import run_agent
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.tasks import create_task
from jigga.runtime.workspaces import read_file, scaffold_workspace, workspace_dir, write_curated


def _result(content: str) -> ModelCallResult:
    return ModelCallResult(status="ok", provider="dry_run", model="m", content=content, dry_run=True, tool_calls=[])


def _write_agent(paths, aid: str) -> None:
    write_yaml(paths.agents / f"{aid}.yaml", {"id": aid, "name": aid, "role": "writer",
               "memory_scope": "task_only", "tools": [], "permissions": {}})


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def test_standalone_agent_gets_workspace_and_writes_output(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _write_agent(paths, "solo")  # in no team
    create_task(paths.tasks, "draft a line", assignee="solo")
    with patch("jigga.runtime.agent.call_model", lambda h, lg, r: _result("HELLO LINE")):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "solo")

    assert workspace_dir(paths.home, "solo").exists()                       # ensured on run
    out = read_file(paths.home, "solo", "shared-context/agent-outputs/solo.md")
    assert "HELLO LINE" in out                                              # write side
    assert "workspace.ensured" in [e["type"] for e in _events(paths)]


def test_team_member_reads_plan_and_writes_to_team_workspace(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    write_yaml(paths.teams / "mt.yaml", {"id": "mt", "name": "MT",
               "agents": [{"id": "marketing_lead", "role": "lead"}],
               "routing": {"default_assignee": "marketing_lead"}})
    _write_agent(paths, "marketing_lead")
    team = load_teams(paths.teams)["mt"]
    scaffold_workspace(paths.home, team)
    write_curated(paths.home, team, "notes/plan.md", "PLAN: ship the dashboard", member="marketing_lead")
    create_task(paths.tasks, "do it", assignee="marketing_lead")

    captured: dict = {}

    def fake(home, logs_dir, request):
        captured["system"] = request.items[0].content
        return _result("done draft")

    with patch("jigga.runtime.agent.call_model", fake):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "marketing_lead")

    # READ: the lead-curated plan was injected into the agent's system prompt
    assert "PLAN: ship the dashboard" in captured["system"]
    # WRITE: output landed in the TEAM workspace; the member's role dir exists
    assert (workspace_dir(paths.home, "mt") / "roles" / "marketing_lead").is_dir()
    assert "done draft" in read_file(paths.home, "mt", "shared-context/agent-outputs/marketing_lead.md")
    # a per-agent workspace was NOT created for a team member
    assert not workspace_dir(paths.home, "marketing_lead").exists()

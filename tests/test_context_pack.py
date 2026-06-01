from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_teams
from jigga.core.io import write_yaml
from jigga.runtime.agent import run_agent
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.context_pack import assemble_agent_context
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.tasks import create_task
from jigga.runtime.team_memory import append_team_memory
from jigga.runtime.workspaces import scaffold_workspace, workspace_dir


def _setup(paths, *, soul=True):
    (paths.home / "USER.md").write_text("# USER\n- Name: RJ\n- Cares about: shipping", encoding="utf-8")
    write_yaml(paths.teams / "mt.yaml", {"id": "mt", "name": "Marketing", "purpose": "Launch copy",
               "agents": [{"id": "lead", "role": "lead"}, {"id": "writer", "role": "copywriter"}],
               "routing": {"default_assignee": "lead"}})
    for a, role in (("lead", "lead"), ("writer", "copywriter")):
        write_yaml(paths.agents / f"{a}.yaml", {"id": a, "name": a.title(), "role": role,
                   "description": f"The {a}.", "memory_scope": "task_only",
                   "tools": ["memory.search"], "permissions": {}})
    team = load_teams(paths.teams)["mt"]
    scaffold_workspace(paths.home, team)
    if not soul:
        (workspace_dir(paths.home, "mt") / "roles" / "writer" / "SOUL.md").unlink()
    append_team_memory(paths.home, "mt", text="ICP is indie devs", type="fact")


def _registry(paths) -> CapabilityRegistry:
    return CapabilityRegistry.load(user_capabilities=paths.capabilities, approvals_dir=paths.policies)


def _assemble(paths, member="writer", **kw):
    agent = load_agents(paths.agents)[member]
    return assemble_agent_context(paths.home, agent, "mt", registry=_registry(paths), **kw)


def test_context_includes_all_layers_in_private_session(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _setup(paths)
    text, layers = _assemble(paths)
    assert text and "RJ" in text                      # USER (principal)
    assert "You are **Writer**" in text                # identity
    assert "lead" in text                              # AGENTS roster (teammate)
    assert "Marketing" in text                         # TEAM charter
    assert "memory.search" in text                     # TOOLS (generated)
    assert "indie devs" in text                        # team memory
    assert {"USER", "identity", "SOUL", "AGENTS", "TEAM", "TOOLS", "MEMORY"}.issubset(set(layers))


def test_restricted_session_omits_private_layers(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _setup(paths)
    text, layers = _assemble(paths, restricted=True)
    assert "USER" not in layers and "MEMORY" not in layers   # private layers withheld
    assert "RJ" not in text and "indie devs" not in text
    # but the agent still knows who it is / its role / tools / team
    assert {"identity", "SOUL", "AGENTS", "TEAM", "TOOLS"}.issubset(set(layers))


def test_authored_file_overrides_generated(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _setup(paths)
    (workspace_dir(paths.home, "mt") / "roles" / "writer" / "TOOLS.md").write_text(
        "# AUTHORED TOOLS POLICY\nNever fabricate stats.", encoding="utf-8")
    text, _ = _assemble(paths)
    assert "AUTHORED TOOLS POLICY" in text          # authored wins
    assert "memory.search —" not in text             # generated list not used


def test_missing_layers_are_skipped(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _setup(paths, soul=False)
    (paths.home / "USER.md").unlink()
    _, layers = _assemble(paths)
    assert "SOUL" not in layers and "USER" not in layers   # absent → skipped, no crash
    assert "identity" in layers                            # generated layer still there


def test_run_agent_injects_context_and_writes_daily_log(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _setup(paths)
    create_task(paths.tasks, "draft copy", assignee="writer", metadata={"team_id": "mt"})
    captured: dict = {}

    def fake(home, logs, request):
        captured["system"] = request.items[0].content
        return ModelCallResult(status="ok", provider="dry_run", model="m", content="drafted", dry_run=True, tool_calls=[])

    with patch("jigga.runtime.agent.call_model", fake):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "writer")

    assert "RJ" in captured["system"] and "indie devs" in captured["system"]   # grounded
    # daily breadcrumb written for next-run continuity
    mem_dir = workspace_dir(paths.home, "mt") / "roles" / "writer" / "memory"
    assert mem_dir.exists() and any(mem_dir.iterdir())


def test_run_agent_restricted_task_withholds_private_context(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _setup(paths)
    create_task(paths.tasks, "public reply", assignee="writer",
                metadata={"team_id": "mt", "restricted_memory": True})
    captured: dict = {}

    def fake(home, logs, request):
        captured["system"] = request.items[0].content
        return ModelCallResult(status="ok", provider="dry_run", model="m", content="ok", dry_run=True, tool_calls=[])

    with patch("jigga.runtime.agent.call_model", fake):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "writer")

    assert "RJ" not in captured["system"]          # principal withheld in a public/group session
    assert "indie devs" not in captured["system"]  # private memory withheld
    assert "You are **Writer**" in captured["system"]   # identity still present

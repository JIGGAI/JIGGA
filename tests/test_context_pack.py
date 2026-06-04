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
    assert "callable tools" in text                    # TOOLS (compact policy — specs carry the catalog)
    assert "indie devs" in text                        # team memory (volatile 'recent' layer)
    assert {"USER", "identity", "SOUL", "AGENTS", "TEAM", "TOOLS", "recent"}.issubset(set(layers))
    # stable → volatile cache boundary sits between the two groups
    from jigga.runtime.context_pack import VOLATILE_BOUNDARY
    assert text.index("callable tools") < text.index(VOLATILE_BOUNDARY) < text.index("indie devs")


def test_restricted_session_omits_private_layers(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _setup(paths)
    text, layers = _assemble(paths, restricted=True)
    assert "USER" not in layers and "recent" not in layers   # private layers withheld
    assert "MEMORY" not in layers
    assert "RJ" not in text and "indie devs" not in text
    # but the agent still knows who it is / its role / tools / team
    assert {"identity", "SOUL", "AGENTS", "TEAM", "TOOLS"}.issubset(set(layers))


def test_authored_tools_md_adds_notes_but_never_hides_grants(tmp_path: Path) -> None:
    """An authored TOOLS.md contributes usage NOTES on top of the generated
    policy — it must never blind the agent to its tools (the #78 failure mode)."""
    paths = init_runtime(tmp_path)
    _setup(paths)
    (workspace_dir(paths.home, "mt") / "roles" / "writer" / "TOOLS.md").write_text(
        "# AUTHORED TOOLS POLICY\nNever fabricate stats.", encoding="utf-8")
    text, _ = _assemble(paths)
    assert "AUTHORED TOOLS POLICY" in text           # authored notes appended...
    assert "Your tool notes" in text
    assert "callable tools" in text                  # ...but the generated policy still ships


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


# --- #86: resident-context budget --------------------------------------------


def test_curated_memory_stable_and_recent_volatile_split(tmp_path: Path) -> None:
    from jigga.core.io import ensure_dir
    from jigga.runtime.context_pack import VOLATILE_BOUNDARY

    paths = init_runtime(tmp_path)
    _setup(paths)
    role_dir = workspace_dir(paths.home, "mt") / "roles" / "writer"
    ensure_dir(role_dir)
    (role_dir / "MEMORY.md").write_text("# MEMORY\nRJ prefers concise replies.", encoding="utf-8")

    text, layers = _assemble(paths)
    assert "MEMORY" in layers and "recent" in layers
    # curated MEMORY.md is STABLE (above boundary); team learnings are volatile (below)
    assert text.index("concise replies") < text.index(VOLATILE_BOUNDARY) < text.index("indie devs")


def test_memory_md_over_budget_gets_consolidation_nudge(tmp_path: Path) -> None:
    from jigga.core.io import ensure_dir
    from jigga.runtime.context_pack import _LAYER_LIMITS

    paths = init_runtime(tmp_path)
    _setup(paths)
    role_dir = workspace_dir(paths.home, "mt") / "roles" / "writer"
    ensure_dir(role_dir)
    big = "# MEMORY\n" + ("fact line\n" * (_LAYER_LIMITS["MEMORY"] // 10))
    (role_dir / "MEMORY.md").write_text(big, encoding="utf-8")

    text, _ = _assemble(paths)
    assert "consolidate/prune entries" in text       # >80% → write-time nudge
    small = "# MEMORY\njust one fact."
    (role_dir / "MEMORY.md").write_text(small, encoding="utf-8")
    text, _ = _assemble(paths)
    assert "consolidate/prune" not in text           # under budget → no nudge


def test_tools_layer_is_compact_not_a_catalog(tmp_path: Path) -> None:
    """The per-tool catalog already ships as API function schemas — the resident
    text must stay a compact policy (this was 54% of a fresh install's context)."""
    paths = init_runtime(tmp_path)
    _setup(paths)
    text, _ = _assemble(paths)
    tools_block = text.split("## Your tools\n", 1)[1].split("\n\n## ", 1)[0]
    assert len(tools_block) < 500
    assert "function specs" in tools_block
    assert "memory.search —" not in tools_block      # no per-tool listing


def test_resident_baseline_under_budget(tmp_path: Path) -> None:
    """The whole fresh-team resident context stays under the tightened budget
    (~6k chars ≈ 1.5k tokens) — the #86 regression guard."""
    paths = init_runtime(tmp_path)
    _setup(paths)
    text, _ = _assemble(paths)
    assert len(text) < 6000


def test_per_layer_caps_clip_oversized_layers(tmp_path: Path) -> None:
    from jigga.runtime.context_pack import _LAYER_LIMITS

    paths = init_runtime(tmp_path)
    _setup(paths)
    (paths.home / "USER.md").write_text("# USER\n" + "RJ fact. " * 500, encoding="utf-8")  # ~4.5KB
    text, _ = _assemble(paths)
    user_block = text.split("## About your principal\n", 1)[1].split("\n\n## ", 1)[0]
    assert len(user_block) <= _LAYER_LIMITS["USER"] + 50          # clipped to the USER cap
    assert "…(truncated)" in user_block

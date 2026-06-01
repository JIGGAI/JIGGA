from __future__ import annotations

from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.config import load_agents
from jigga.core.io import write_yaml
from jigga.core.models import TeamConfig
from jigga.runtime.dispatcher import RuntimeContext, _remember_handler, _search_memory_handler
from jigga.runtime.memory_index import search_memory
from jigga.runtime.team_memory import (
    append_role_memory,
    append_team_memory,
    pin_entry,
    read_pinned,
    read_team_memory,
)
from jigga.runtime.workspaces import scaffold_workspace


def _team(paths) -> TeamConfig:
    team = TeamConfig(id="mt", name="MT", agents=[{"id": "mt-lead", "role": "lead"}],
                      routing={"default_assignee": "mt-lead"})
    scaffold_workspace(paths.home, team)
    return team


# --- write API -------------------------------------------------------------


def test_append_read_and_pin(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths)
    e1 = append_team_memory(paths.home, "mt", text="The launch ships Tuesday.", type="fact", tags=["launch"])
    append_team_memory(paths.home, "mt", text="Prefer plain English in copy.", type="preference")
    entries = read_team_memory(paths.home, "mt")
    assert [e["text"] for e in entries] == ["The launch ships Tuesday.", "Prefer plain English in copy."]

    assert pin_entry(paths.home, "mt", e1["id"]) is not None
    pinned = read_pinned(paths.home, "mt")
    assert len(pinned) == 1 and pinned[0]["text"] == "The launch ships Tuesday."


def test_role_memory_append(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths)
    append_role_memory(paths.home, "mt", "mt-lead", "Learned: the CFO wants ROI framing.")
    text = (tmp_path / "workspaces" / "mt" / "roles" / "mt-lead" / "MEMORY.md").read_text()
    assert "ROI framing" in text


# --- searchable + team-scoped ----------------------------------------------


def test_team_memory_is_searchable_and_scoped(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths)
    # a second team with its own secret
    other = TeamConfig(id="other", name="Other", agents=[{"id": "other-lead", "role": "lead"}],
                       routing={"default_assignee": "other-lead"})
    scaffold_workspace(paths.home, other)
    append_team_memory(paths.home, "mt", text="kangaroo plan for the mt launch")
    append_team_memory(paths.home, "other", text="kangaroo plan for the other team")

    # unscoped: finds both
    assert len(search_memory(paths.memory, "kangaroo")) == 2
    # team-scoped: only mt's
    mt_only = search_memory(paths.memory, "kangaroo", team="mt")
    assert len(mt_only) == 1 and mt_only[0]["layer"] == "team:mt"


# --- capabilities ----------------------------------------------------------


def _runtime(paths, agent_id="solo") -> RuntimeContext:
    write_yaml(paths.agents / f"{agent_id}.yaml", {"id": agent_id, "name": agent_id, "role": "x",
               "memory_scope": "task_only", "tools": [], "permissions": {}})
    agent = load_agents(paths.agents)[agent_id]
    return RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs, sessions_dir=paths.home / "sessions")


def test_remember_then_search_via_capabilities(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    runtime = _runtime(paths, "solo")  # team-less → its own workspace (id == solo)
    out = _remember_handler(None, None, {"text": "Remember the platypus protocol.", "type": "fact"}, {}, runtime)
    assert out["team"] == "solo" and out["remembered"]

    found = _search_memory_handler(None, None, {"query": "platypus"}, {}, runtime)
    assert found["results"] and found["results"][0]["layer"] == "team:solo"


def test_remember_requires_text(tmp_path: Path) -> None:
    import pytest
    paths = init_runtime(tmp_path)
    runtime = _runtime(paths, "solo")
    with pytest.raises(ValueError):
        _remember_handler(None, None, {"text": "   "}, {}, runtime)


def test_capabilities_registered(tmp_path: Path) -> None:
    from jigga.runtime.capabilities import CapabilityRegistry
    init_runtime(tmp_path)
    reg = CapabilityRegistry.load(user_capabilities=tmp_path / "capabilities", approvals_dir=tmp_path / "policies")
    assert reg.resolve_action("memory.remember").handler == "runtime.remember"

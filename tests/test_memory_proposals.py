from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.config import load_agents
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.dispatcher import RuntimeContext, _remember_handler
from jigga.runtime.memory_proposals import apply_proposal, list_proposals, sensitive_requires_approval
from jigga.runtime.team_memory import read_team_memory


def _runtime(paths, agent_id="solo") -> RuntimeContext:
    write_yaml(paths.agents / f"{agent_id}.yaml", {"id": agent_id, "name": agent_id, "role": "x",
               "memory_scope": "task_only", "tools": [], "permissions": {}})
    return RuntimeContext(agent=load_agents(paths.agents)[agent_id], home=paths.home,
                          logs_dir=paths.logs, sessions_dir=paths.home / "sessions")


def _set(paths, **memory_cfg) -> None:
    cfg = read_yaml(paths.config)
    cfg["memory"] = {**(cfg.get("memory") or {}), **memory_cfg}
    write_yaml(paths.config, cfg)


# --- gating off by default -------------------------------------------------


def test_off_by_default_writes_directly(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    runtime = _runtime(paths)
    out = _remember_handler(None, None, {"text": "user prefers dark mode", "type": "preference"}, {}, runtime)
    assert out.get("remembered")                                  # written, not proposed
    assert read_team_memory(paths.home, "solo")[0]["text"] == "user prefers dark mode"
    assert list_proposals(paths.home) == []


def test_require_approval_parks_sensitive_types(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set(paths, require_approval=True)
    runtime = _runtime(paths)
    assert sensitive_requires_approval(paths.home, "fact") is True
    assert sensitive_requires_approval(paths.home, "note") is False   # not sensitive

    out = _remember_handler(None, None, {"text": "user's manager is Dana", "type": "relationship"}, {}, runtime)
    assert out["status"] == "pending_approval" and out.get("proposed")
    assert read_team_memory(paths.home, "solo") == []             # NOT written yet
    pending = list_proposals(paths.home)
    assert len(pending) == 1 and pending[0]["text"] == "user's manager is Dana"

    # a non-sensitive type still writes directly even with approval on
    direct = _remember_handler(None, None, {"text": "ran the build", "type": "note"}, {}, runtime)
    assert direct.get("remembered")


def test_approve_commits_reject_discards(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set(paths, require_approval=True)
    runtime = _runtime(paths)
    p1 = _remember_handler(None, None, {"text": "fact one", "type": "fact"}, {}, runtime)["proposed"]
    p2 = _remember_handler(None, None, {"text": "fact two", "type": "fact"}, {}, runtime)["proposed"]

    approved = apply_proposal(paths.home, p1, approve=True)
    assert approved["status"] == "approved" and approved["memory_id"]
    assert [e["text"] for e in read_team_memory(paths.home, "solo")] == ["fact one"]

    rejected = apply_proposal(paths.home, p2, approve=False)
    assert rejected["status"] == "rejected"
    assert [e["text"] for e in read_team_memory(paths.home, "solo")] == ["fact one"]  # p2 not committed
    assert list_proposals(paths.home) == []                       # both resolved


# --- CLI -------------------------------------------------------------------


def test_cli_proposals_list_and_approve(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    _set(paths, require_approval=True)
    runtime = _runtime(paths)
    pid = _remember_handler(None, None, {"text": "secret fact", "type": "fact"}, {}, runtime)["proposed"]

    assert main(["--home", str(tmp_path), "memory", "proposals", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == pid

    assert main(["--home", str(tmp_path), "memory", "approve", pid]) == 0
    assert "Approved" in capsys.readouterr().out
    assert read_team_memory(paths.home, "solo")[0]["text"] == "secret fact"

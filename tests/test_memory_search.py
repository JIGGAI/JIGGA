from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.memory import write_memory_result
from jigga.runtime.memory_index import rebuild_index, search_memory


def _seed(paths) -> None:
    write_memory_result(paths.memory, paths.logs, "note", "The launch tweet mentions the cost dashboard.", {"agent": "lead"})
    write_memory_result(paths.memory, paths.logs, "note", "Quarterly OKRs focus on retention and onboarding.", {"agent": "lead"})
    (paths.memory / "summaries").mkdir(exist_ok=True)
    (paths.memory / "summaries" / "week.md").write_text("Summary: shipped the cost dashboard beta.", encoding="utf-8")


def test_search_ranks_matching_documents(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _seed(paths)
    hits = search_memory(paths.memory, "cost dashboard")
    assert hits, "expected matches"
    # both the raw note and the summary mention 'cost dashboard'
    blobs = " ".join(h["path"] + h["snippet"] for h in hits).lower()
    assert "dashboard" in blobs
    # a term that only appears in the OKR note finds just that one
    okr = search_memory(paths.memory, "retention")
    assert len(okr) == 1 and "retention" in okr[0]["snippet"].lower()


def test_search_no_match_is_empty(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _seed(paths)
    assert search_memory(paths.memory, "zzznonexistentterm") == []
    assert search_memory(paths.memory, "") == []


def test_reindex_counts_documents(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _seed(paths)
    assert rebuild_index(paths.memory) == 3  # 2 raw + 1 summary


def test_scope_restricts_results(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _seed(paths)
    write_yaml(paths.memory / "memory_scopes.yaml", {
        "memory_scopes": {"summaries_only": {"name": "S", "description": "x", "includes": ["memory/summaries"]}}})
    hits = search_memory(paths.memory, "dashboard", scope="summaries_only")
    assert hits and all(h["layer"] == "summaries" for h in hits)   # raw note excluded by scope


def test_index_refreshes_when_memory_changes(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _seed(paths)
    assert search_memory(paths.memory, "kangaroo") == []
    write_memory_result(paths.memory, paths.logs, "note", "A wild kangaroo appeared.", {})
    assert search_memory(paths.memory, "kangaroo")               # stale index auto-rebuilt


# --- capability + CLI ------------------------------------------------------


def test_memory_search_capability_registered(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    reg = CapabilityRegistry.load(user_capabilities=tmp_path / "capabilities", approvals_dir=tmp_path / "policies")
    cap = reg.resolve_action("memory.search")
    assert cap is not None and cap.handler == "runtime.search_memory"


def test_cli_memory_search_json(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    _seed(paths)
    assert main(["--home", str(tmp_path), "memory", "search", "dashboard", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any("dashboard" in (r["snippet"] or "").lower() for r in payload)

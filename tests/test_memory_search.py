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


# --- #96: task history + workspace coverage -----------------------------------


def test_search_finds_channel_task_history(tmp_path: Path) -> None:
    """Every channel chat is a task — 'what did RJ ask me yesterday' must be
    one zero-token search away (the Hermes contract behind #86's small
    resident baseline)."""
    from jigga.runtime.tasks import create_task, set_task_state

    paths = init_runtime(tmp_path)
    task = create_task(paths.tasks, "telegram message from RJ",
                       description="Message received via telegram: can you book the dentist for Thursday?",
                       assignee="assistant", metadata={"channel": "telegram", "sender": "RJ"})
    set_task_state(paths.tasks, task.id, "completed")

    results = search_memory(paths.memory, "dentist Thursday", rebuild=True)
    assert results and results[0]["layer"] == "tasks"
    assert "dentist" in results[0]["snippet"].lower()


def test_search_finds_archived_tasks(tmp_path: Path) -> None:
    import json as _json

    paths = init_runtime(tmp_path)
    archive = paths.tasks / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "task_old.json").write_text(_json.dumps({
        "id": "task_old", "title": "telegram message from RJ",
        "description": "remind me about the quarterly tax filing",
        "assignee": "assistant", "state": "completed", "metadata": {}}), encoding="utf-8")

    results = search_memory(paths.memory, "quarterly tax filing", rebuild=True)
    assert results and results[0]["layer"] == "tasks"


def test_search_finds_workspace_daily_logs_outputs_and_decisions(tmp_path: Path) -> None:
    from jigga.core.io import ensure_dir

    paths = init_runtime(tmp_path)
    ws = paths.home / "workspaces" / "mt"
    ensure_dir(ws / "roles" / "writer" / "memory")
    ensure_dir(ws / "shared-context" / "agent-outputs")
    (ws / "roles" / "writer" / "memory" / "2026-06-01.md").write_text(
        "Drafted the solstice launch tweet.", encoding="utf-8")
    (ws / "shared-context" / "agent-outputs" / "writer.md").write_text(
        "## output\nFinal launch copy: midnight aurora edition.", encoding="utf-8")
    (ws / "shared-context" / "handoffs.jsonl").write_text(
        '{"from": "writer", "to": "editor", "when": "draft_ready", "evidence": "aurora copy v2"}\n',
        encoding="utf-8")

    daily = search_memory(paths.memory, "solstice launch tweet", team="mt", rebuild=True)
    assert any(r["layer"] == "role:writer" for r in daily) is False  # role:writer ≠ team mt gate
    daily_unscoped = search_memory(paths.memory, "solstice launch tweet", rebuild=True)
    assert any(r["layer"] == "role:writer" for r in daily_unscoped)

    outputs = search_memory(paths.memory, "midnight aurora", team="mt")
    assert any(r["layer"] == "team:mt" for r in outputs)
    decisions = search_memory(paths.memory, "draft_ready aurora", team="mt")
    assert any(r["layer"] == "team:mt" for r in decisions)


def test_team_gate_still_hides_other_teams(tmp_path: Path) -> None:
    """#96 must not weaken scoping: another team's workspace stays invisible
    to a team-filtered search; tasks are global like the memory tree."""
    from jigga.core.io import ensure_dir

    paths = init_runtime(tmp_path)
    other = paths.home / "workspaces" / "other_team" / "shared-context" / "memory"
    ensure_dir(other)
    (other / "team.jsonl").write_text('{"text": "secret zebra initiative"}\n', encoding="utf-8")

    hidden = search_memory(paths.memory, "secret zebra initiative", team="mt", rebuild=True)
    assert hidden == []
    visible = search_memory(paths.memory, "secret zebra initiative", team="other_team")
    assert visible and visible[0]["layer"] == "team:other_team"

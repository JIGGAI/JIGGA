from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.models import TeamConfig
from jigga.runtime.compaction import compact_memory, maybe_compact
from jigga.runtime.workspaces import scaffold_workspace

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _raw(paths, name: str, days_ago: int) -> None:
    (paths.memory / "raw").mkdir(exist_ok=True)
    (paths.memory / "raw" / name).write_text(
        json.dumps({"id": name[:-5], "time": _iso(days_ago), "type": "note", "content": "x"}), encoding="utf-8")


def _team_jsonl(paths, team_id: str, rows: list[tuple[str, int]]) -> Path:
    scaffold_workspace(paths.home, TeamConfig(id=team_id, name=team_id, agents=[{"id": f"{team_id}-lead"}],
                                              routing={"default_assignee": f"{team_id}-lead"}))
    path = paths.home / "workspaces" / team_id / "shared-context" / "memory" / "team.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"id": f"m{i}", "time": _iso(d), "text": t}) + "\n"
                            for i, (t, d) in enumerate(rows)), encoding="utf-8")
    return path


def test_compact_archives_old_raw_keeps_recent(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _raw(paths, "old.json", days_ago=60)
    _raw(paths, "new.json", days_ago=5)
    summary = compact_memory(paths.home, now=NOW)  # default raw_retention_days=30
    assert summary["raw_archived"] == ["old.json"]
    assert not (paths.memory / "raw" / "old.json").exists()
    assert (paths.memory / "raw" / "archive" / "old.json").exists()
    assert (paths.memory / "raw" / "new.json").exists()


def test_compact_archives_stale_facts_keeps_fresh(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    path = _team_jsonl(paths, "mt", [("ancient fact", 200), ("fresh fact", 3)])  # default fact_stale_days=90
    summary = compact_memory(paths.home, now=NOW)
    assert summary["facts_archived"] == 1
    remaining = [json.loads(line)["text"] for line in path.read_text().splitlines() if line.strip()]
    assert remaining == ["fresh fact"]
    archived = (paths.home / "workspaces" / "mt" / "shared-context" / "memory" / "team.archive.jsonl").read_text()
    assert "ancient fact" in archived


def test_compact_archives_finished_tasks(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    (paths.tasks).mkdir(exist_ok=True)
    (paths.tasks / "done.json").write_text(json.dumps(
        {"id": "done", "title": "t", "state": "completed", "created_at": _iso(50), "updated_at": _iso(50)}), encoding="utf-8")
    (paths.tasks / "pending.json").write_text(json.dumps(
        {"id": "pending", "title": "t", "state": "pending", "created_at": _iso(50), "updated_at": _iso(50)}), encoding="utf-8")
    summary = compact_memory(paths.home, now=NOW)  # default task_retention_days=30
    assert summary["tasks_archived"] == ["done.json"]
    assert (paths.tasks / "archive" / "done.json").exists()
    assert (paths.tasks / "pending.json").exists()                       # pending never archived


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _raw(paths, "old.json", days_ago=60)
    summary = compact_memory(paths.home, now=NOW, dry_run=True)
    assert summary["raw_archived"] == ["old.json"] and summary["dry_run"] is True
    assert (paths.memory / "raw" / "old.json").exists()                  # not moved


def test_maybe_compact_is_rate_limited(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _raw(paths, "old.json", days_ago=60)
    first = maybe_compact(paths.home, now=NOW)
    assert first is not None and first["raw_archived"] == ["old.json"]
    # within interval_hours (default 24) → guarded, no second run
    assert maybe_compact(paths.home, now=NOW + timedelta(hours=1)) is None
    # past the interval → runs again
    assert maybe_compact(paths.home, now=NOW + timedelta(hours=25)) is not None


def test_cli_memory_compact(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    _raw(paths, "old.json", days_ago=60)
    assert main(["--home", str(tmp_path), "memory", "compact", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["raw_archived"] == ["old.json"]


def test_compaction_prunes_archived_task_from_index(tmp_path: Path) -> None:
    """End-to-end: a task created through the normal path (so the index exists)
    that ages to completed is removed from the index when compaction archives it."""
    from datetime import datetime, timedelta, timezone
    from jigga.core.io import read_json
    from jigga.runtime.tasks import _index_path, create_task, find_task, set_task_state

    paths = init_runtime(tmp_path)
    t = create_task(paths.tasks, "old", assignee="alpha")
    set_task_state(paths.tasks, t.id, "completed")
    # backdate updated_at so it's past the retention cutoff
    task_file = paths.tasks / f"{t.id}.json"
    data = read_json(task_file)
    data["updated_at"] = (datetime.now(timezone.utc) - timedelta(days=999)).isoformat()
    (task_file).write_text(json.dumps(data), encoding="utf-8")

    assert t.id in read_json(_index_path(paths.tasks))      # indexed before compaction
    compact_memory(paths.home)
    assert t.id not in read_json(_index_path(paths.tasks))  # pruned from the index
    assert find_task(paths.tasks, t.id) is None             # and no longer findable


def test_compaction_tolerates_corrupt_team_memory_line(tmp_path: Path) -> None:
    """One corrupt line in a team's team.jsonl must not crash the whole
    compaction pass (it runs on the supervisor heartbeat)."""
    from jigga.core.models import TeamConfig
    from jigga.runtime.workspaces import scaffold_workspace, workspace_dir
    paths = init_runtime(tmp_path)
    team = TeamConfig(id="t", name="t", agents=[{"id": "t-lead"}])
    scaffold_workspace(paths.home, team)
    mem = workspace_dir(paths.home, "t") / "shared-context" / "memory" / "team.jsonl"
    mem.parent.mkdir(parents=True, exist_ok=True)
    mem.write_text(
        json.dumps({"time": "2999-01-01T00:00:00+00:00", "text": "fresh"}) + "\n"
        + "{ half-written line after a crash\n", encoding="utf-8")
    summary = compact_memory(paths.home)            # must not raise
    assert "facts_archived" in summary

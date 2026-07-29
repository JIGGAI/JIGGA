"""Crash-recovery sweep: stale claimed/running tasks and stuck v2 workflow
nodes are marked failed (visibly, never silently retried); fresh work is
untouched; the supervisor tick runs the sweep contained."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from jigga.commands.init import init_runtime
from jigga.core.io import read_json, write_json, write_yaml
from jigga.core.paths import get_paths
from jigga.runtime.recovery import sweep_stale
from jigga.runtime.supervisor import supervisor_tick
from jigga.runtime.tasks import create_task, list_tasks, set_task_state

# Real wall-clock: create_task/set_task_state stamp datetime.now, so a fixed
# reference time would make freshly-created "fresh" records look stale.
_NOW = datetime.now(timezone.utc)


def _age_task(paths, task_id: str, *, hours: float) -> None:
    path = paths.tasks / f"{task_id}.json"
    record = read_json(path)
    record["updated_at"] = (_NOW - timedelta(hours=hours)).isoformat()
    write_json(path, record)


def test_stale_claimed_and_running_tasks_fail_fresh_untouched(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    stale = create_task(paths.tasks, title="stale", assignee="daily_briefing_agent")
    set_task_state(paths.tasks, stale.id, "claimed")
    set_task_state(paths.tasks, stale.id, "running")
    _age_task(paths, stale.id, hours=5)
    fresh = create_task(paths.tasks, title="fresh", assignee="daily_briefing_agent")
    set_task_state(paths.tasks, fresh.id, "claimed")
    pending = create_task(paths.tasks, title="pending old", assignee="daily_briefing_agent")
    _age_task(paths, pending.id, hours=50)  # pending is not a half-state

    result = sweep_stale(get_paths(tmp_path), now=_NOW)
    assert result["tasks"] == [stale.id]
    states = {t.id: t.state for t in list_tasks(paths.tasks)}
    assert states[stale.id] == "failed"
    assert states[fresh.id] == "claimed"
    assert states[pending.id] == "pending"


def test_threshold_configurable(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.config, {"recovery": {"max_stale_minutes": 10}})
    task = create_task(paths.tasks, title="t", assignee="a")
    set_task_state(paths.tasks, task.id, "running")
    _age_task(paths, task.id, hours=0.5)  # 30 min > 10 min threshold
    assert sweep_stale(get_paths(tmp_path), now=_NOW)["tasks"] == [task.id]


def _wedged_run(paths, workflow_id: str = "wedged") -> str:
    """A v2 run whose first node is stuck `running` (simulated crash) with a
    live error edge to a recovery node."""
    (paths.workflows / f"{workflow_id}.yaml").write_text(yaml.safe_dump({
        "id": workflow_id, "name": "Wedged",
        "nodes": [
            {"id": "boom", "agent": "daily_briefing_agent", "action": "calendar.list_events"},
            {"id": "cleanup", "agent": "daily_briefing_agent", "action": "notifications.send",
             "input": {"message": "recovered"}},
        ],
        "edges": [{"from": "boom", "to": "cleanup", "on": "error"}],
    }), encoding="utf-8")
    run_dir = paths.runs / "workflows" / workflow_id / "workflow_run_x1"
    run_dir.mkdir(parents=True)
    write_json(run_dir / "run.json", {
        "id": "workflow_run_x1", "workflow_id": workflow_id, "engine": "v2",
        "status": "running", "created_at": (_NOW - timedelta(hours=6)).isoformat(),
        "updated_at": (_NOW - timedelta(hours=6)).isoformat(), "completed_at": None,
        "run_dir": str(run_dir), "trace_id": None, "trigger": {},
        "nodes": {"boom": {"status": "running",
                           "started_at": (_NOW - timedelta(hours=6)).isoformat()},
                  "cleanup": {"status": "pending"}},
        "outputs": {},
    })
    return "workflow_run_x1"


def test_stuck_workflow_node_recovered_and_run_advances(tmp_path: Path) -> None:
    from jigga.runtime.workflow_engine import advance_run, load_run

    paths = init_runtime(tmp_path, examples=True)
    run_id = _wedged_run(paths)
    jp = get_paths(tmp_path)
    result = sweep_stale(jp, now=_NOW)
    assert result["nodes"] == [f"{run_id}:boom"]
    record = load_run(jp, run_id)
    assert record["nodes"]["boom"]["status"] == "failed"
    assert "crash sweep" in record["nodes"]["boom"]["error"]
    # The wedge is broken: the next advance fires the error edge and completes.
    finished = advance_run(jp, record)
    assert finished["nodes"]["cleanup"]["status"] == "done"
    assert finished["status"] == "completed"


def test_fresh_running_node_untouched(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    run_id = _wedged_run(paths)
    jp = get_paths(tmp_path)
    record_path = paths.runs / "workflows" / "wedged" / run_id / "run.json"
    record = read_json(record_path)
    record["nodes"]["boom"]["started_at"] = (_NOW - timedelta(minutes=1)).isoformat()
    write_json(record_path, record)
    assert sweep_stale(jp, now=_NOW)["nodes"] == []


def test_supervisor_tick_runs_the_sweep(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    task = create_task(paths.tasks, title="stale", assignee="daily_briefing_agent")
    set_task_state(paths.tasks, task.id, "running")
    _age_task(paths, task.id, hours=5)
    supervisor_tick(paths.home)
    assert {t.id: t.state for t in list_tasks(paths.tasks)}[task.id] == "failed"
    events = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines()]
    assert any(e.get("type") == "task.recovered" for e in events)
    assert any(e.get("type") == "recovery.swept" for e in events)


def test_corrupt_run_file_does_not_break_sweep(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    bad = paths.runs / "workflows" / "junk" / "workflow_run_bad"
    bad.mkdir(parents=True)
    (bad / "run.json").write_text("{not json", encoding="utf-8")
    assert sweep_stale(get_paths(tmp_path), now=_NOW) == {"tasks": [], "nodes": []}

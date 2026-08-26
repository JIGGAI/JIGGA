from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.config import load_teams
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.agent import run_agent
from jigga.runtime.handoffs import fire_handoffs, read_decision_log
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.tasks import create_task, list_tasks, tasks_for_agent
from jigga.runtime.workspaces import scaffold_workspace


def _result(content: str) -> ModelCallResult:
    return ModelCallResult(status="ok", provider="dry_run", model="m", content=content, dry_run=True, tool_calls=[])


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _team(paths, *, handoffs, members=("strategist", "writer", "editor")) -> None:
    write_yaml(paths.teams / "ct.yaml", {
        "id": "ct", "name": "Content Team",
        "agents": [{"id": m, "role": m} for m in members],
        "routing": {"default_assignee": members[0], "handoffs": handoffs},
    })
    for m in members:
        write_yaml(paths.agents / f"{m}.yaml", {"id": m, "name": m, "role": "x",
                   "memory_scope": "task_only", "tools": [], "permissions": {}})
    scaffold_workspace(paths.home, load_teams(paths.teams)["ct"])


# --- fire_handoffs unit ----------------------------------------------------


def test_fire_creates_task_logs_decision_and_audits(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, handoffs=[{"from": "strategist", "to": "writer", "when": "core_message_ready"}])

    created = fire_handoffs(paths.home, paths.logs, paths.tasks, paths.teams,
                            team_id="ct", from_member="strategist")
    assert len(created) == 1 and created[0]["assignee"] == "writer"
    # the new task is pending for the `to` member, with team + hop metadata
    pend = tasks_for_agent(paths.tasks, "writer")
    assert len(pend) == 1
    assert pend[0].metadata["team_id"] == "ct" and pend[0].metadata["handoff_hops"] == 1

    # decision log is a file you can read
    log = read_decision_log(paths.home, "ct")
    assert len(log) == 1 and log[0]["from"] == "strategist" and log[0]["to"] == "writer"
    assert log[0]["when"] == "core_message_ready"

    fired = [e for e in _events(paths) if e["type"] == "team.handoff.fired"]
    assert fired and fired[0]["details"]["to"] == "writer"


def test_no_matching_handoff_is_noop(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, handoffs=[{"from": "strategist", "to": "writer", "when": "x"}])
    # editor has no outgoing handoffs
    assert fire_handoffs(paths.home, paths.logs, paths.tasks, paths.teams,
                         team_id="ct", from_member="editor") == []
    assert read_decision_log(paths.home, "ct") == []


def test_explicit_signal_filters(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, handoffs=[
        {"from": "strategist", "to": "writer", "when": "draft"},
        {"from": "strategist", "to": "editor", "when": "review"},
    ])
    created = fire_handoffs(paths.home, paths.logs, paths.tasks, paths.teams,
                            team_id="ct", from_member="strategist", signal="review")
    assert len(created) == 1 and created[0]["assignee"] == "editor"


def test_hop_guard_blocks_runaway_chain(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    cfg = read_yaml(paths.config)
    cfg["teams"] = {"handoff_max_hops": 2}
    write_yaml(paths.config, cfg)
    _team(paths, handoffs=[{"from": "strategist", "to": "writer", "when": "x"}])

    blocked = fire_handoffs(paths.home, paths.logs, paths.tasks, paths.teams,
                            team_id="ct", from_member="strategist", hops=2)
    assert blocked == []
    assert any(e["type"] == "team.handoff.blocked" for e in _events(paths))


# --- integration: completion drives the chain ------------------------------


def test_agent_completion_fires_handoff_to_next_member(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, handoffs=[
        {"from": "strategist", "to": "writer", "when": "core_message_ready"},
        {"from": "writer", "to": "editor", "when": "draft_ready"},
    ])
    # seed the first member's team task (as run_team would)
    create_task(paths.tasks, "kick off", assignee="strategist", metadata={"team_id": "ct"})

    with patch("jigga.runtime.agent.call_model", lambda h, lg, r: _result("message drafted")):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "strategist")

    # strategist's completion handed off to writer
    writer_tasks = tasks_for_agent(paths.tasks, "writer")
    assert len(writer_tasks) == 1
    assert writer_tasks[0].metadata["handoff_from"] == "strategist"

    # run the writer → it should hand off to editor (hop 2), chain continues
    with patch("jigga.runtime.agent.call_model", lambda h, lg, r: _result("draft done")):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "writer")
    editor_tasks = tasks_for_agent(paths.tasks, "editor")
    assert len(editor_tasks) == 1 and editor_tasks[0].metadata["handoff_hops"] == 2

    # the decision log captured both hops, in order
    log = read_decision_log(paths.home, "ct")
    assert [(e["from"], e["to"]) for e in log] == [("strategist", "writer"), ("writer", "editor")]


# --- guards + CLI ----------------------------------------------------------


def test_non_team_task_completion_does_not_fire_handoffs(tmp_path: Path) -> None:
    """A completed task with no team_id must not trigger any handoff (the
    _maybe_fire_handoffs guard). Otherwise ordinary tasks would spawn handoff
    tasks."""
    paths = init_runtime(tmp_path)
    _team(paths, handoffs=[{"from": "strategist", "to": "writer", "when": "x"}])
    # a plain task for strategist with NO team_id metadata
    create_task(paths.tasks, "solo task", assignee="strategist")

    with patch("jigga.runtime.agent.call_model", lambda h, lg, r: _result("done")):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "strategist")

    assert tasks_for_agent(paths.tasks, "writer") == []          # no handoff fired
    assert read_decision_log(paths.home, "ct") == []
    assert not any(e["type"] == "team.handoff.fired" for e in _events(paths))


def test_cli_team_handoff_and_decisions(tmp_path: Path, capsys) -> None:
    """End-to-end through main(): `team handoff` creates the task + logs the
    decision; `team decisions` reads it back. Guards the CLI subcommand wiring
    (the H0 dead-branch class)."""
    from jigga.cli import main

    paths = init_runtime(tmp_path)
    _team(paths, handoffs=[{"from": "strategist", "to": "writer", "when": "core_message_ready"}])

    assert main(["--home", str(tmp_path), "team", "handoff", "ct",
                 "--from", "strategist", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out and out[0]["assignee"] == "writer"
    assert tasks_for_agent(paths.tasks, "writer")                # task really created

    assert main(["--home", str(tmp_path), "team", "decisions", "ct", "--json"]) == 0
    log = json.loads(capsys.readouterr().out)
    assert log and log[0]["from"] == "strategist" and log[0]["to"] == "writer"


def test_a_lane_managed_team_does_not_spawn_handoff_tickets(tmp_path: Path) -> None:
    """One ticket travels the board. The 2026-08-26 end-to-end produced four
    tickets for one request, three of them handoff spawn."""
    paths = init_runtime(tmp_path)
    # `handoffs` nests under `routing`, matching `eligible_handoffs` (which reads
    # `team.routing["handoffs"]`) and this file's `_team` helper above — a
    # top-level `handoffs:` key is dropped by TeamConfig.from_dict's field
    # filter and would make this test pass trivially, before any fix.
    write_yaml(paths.teams / "eng.yaml", {
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-dev", "role": "dev"}, {"id": "eng-test", "role": "test"}],
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"}, {"id": "done"}],
        "routing": {"handoffs": [{"from": "eng-dev", "to": "eng-test", "when": "verify"}]},
    })
    before = len(list_tasks(paths.tasks))

    created = fire_handoffs(paths.home, paths.logs, paths.tasks, paths.teams,
                            team_id="eng", from_member="eng-dev")

    assert created == []
    assert len(list_tasks(paths.tasks)) == before


def test_malformed_routing_does_not_crash(tmp_path: Path) -> None:
    """A bad team YAML (handoffs not a list, non-dict entries, routing not a
    dict) must not crash fire_handoffs — it runs on the supervisor tick path."""
    from jigga.core.models import TeamConfig
    from jigga.runtime.handoffs import eligible_handoffs
    for routing in ({"handoffs": {"oops": "x"}}, {"handoffs": ["junk", 5]},
                    {"handoffs": None}, "not-a-dict"):
        team = TeamConfig(id="t", name="t")
        team.routing = routing
        assert eligible_handoffs(team, "a", None) == []

    paths = init_runtime(tmp_path)
    write_yaml(paths.teams / "bad.yaml", {"id": "bad", "name": "bad",
               "agents": [{"id": "a"}], "routing": {"handoffs": ["junk"]}})
    # must return [] rather than raise
    assert fire_handoffs(paths.home, paths.logs, paths.tasks, paths.teams,
                         team_id="bad", from_member="a") == []

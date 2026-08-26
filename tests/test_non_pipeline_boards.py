"""A board is not automatically the engineering pipeline.

`lane_transitions` filters the default rules down to lanes the team actually
declares and nulls the bounce lane when `backlog` is absent; `handlers` looked
for a lane literally named `ready-for-pr`; `fire_handoffs` stood down for ANY
team with lanes. Composed, a lane-bearing team whose vocabulary is not the
pipeline — marketing's brief/drafting/review/published, or the `lanes: true`
shorthand — got no transitions, no bounce lane, no closable lane and no handoff
tasks: every completed run went straight to `blocked` on the FIRST run, with no
route back. These teams keep the behaviour they had before this feature.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jigga.commands.init import init_runtime
from jigga.core.config import load_teams
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.agent import run_agent
from jigga.runtime.capabilities import bundled_capabilities
from jigga.runtime.handlers import _tickets_handler
from jigga.runtime.lanes import close_lane, is_lifecycle_managed
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.tasks import create_task, find_task, list_tasks

MARKETING_LANES = [{"id": "brief"}, {"id": "drafting"}, {"id": "review"}, {"id": "published"}]
PIPELINE_LANES = [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
                  {"id": "ready-for-pr"}, {"id": "done"}]


def _result(content="done") -> ModelCallResult:
    return ModelCallResult(status="ok", provider="dry_run", model="m",
                           content=content, dry_run=True, tool_calls=[])


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _write_team(paths, team_id, *, lanes, agents, routing=None, transitions=None):
    data = {"id": team_id, "name": team_id, "agents": agents, "lanes": lanes}
    if routing is not None:
        data["routing"] = routing
    if transitions is not None:
        data["lane_transitions"] = transitions
    write_yaml(paths.teams / f"{team_id}.yaml", data)
    for member in agents:
        aid = member["id"]
        write_yaml(paths.agents / f"{aid}.yaml", {
            "id": aid, "name": aid, "role": "r", "memory_scope": "task_only",
            "tools": [], "permissions": {}, "permission_mode": "autonomous"})
    return load_teams(paths.teams)[team_id]


def _marketing(paths, *, routing=None, lanes=MARKETING_LANES):
    return _write_team(paths, "mk", lanes=lanes, routing=routing, agents=[
        {"id": "mk-strategy", "role": "strategy"},
        {"id": "mk-drafting", "role": "drafting"},
        {"id": "mk-review", "role": "review"}])


# --- the predicate ----------------------------------------------------------


def test_the_pipeline_board_is_lifecycle_managed(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    team = _write_team(paths, "eng", lanes=PIPELINE_LANES, agents=[
        {"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"},
        {"id": "eng-test", "role": "test"}])
    assert is_lifecycle_managed(team)


@pytest.mark.parametrize("lanes, why", [
    (MARKETING_LANES, "no rule can target any of these lanes"),
    (True, "the shorthand defaults share no lane with the pipeline rules"),
    ([{"id": "backlog"}, {"id": "in-progress"}], "no terminal done lane"),
])
def test_a_board_that_cannot_run_the_lifecycle_says_so(tmp_path: Path, lanes, why) -> None:
    paths = init_runtime(tmp_path)
    assert not is_lifecycle_managed(_marketing(paths, lanes=lanes)), why


# --- the outcome path -------------------------------------------------------


def test_a_marketing_ticket_completes_instead_of_blocking(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _marketing(paths)
    create_task(paths.tasks, "write the launch post", assignee="mk-drafting",
                lane="drafting", metadata={"team_id": "mk"})

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "mk-drafting")

    ticket = list_tasks(paths.tasks)[0]
    assert ticket.state == "completed"      # not `blocked`, which is irreversible
    assert ticket.lane == "drafting"        # untouched
    assert ticket.assignee == "mk-drafting"
    assert not [e for e in _events(paths) if e["type"] == "ticket.blocked"]


def test_a_marketing_team_still_fires_its_handoffs(tmp_path: Path) -> None:
    # Its only coordination mechanism. Standing handoffs down for it while
    # giving it no tickets.handoff route would stop its board dead.
    paths = init_runtime(tmp_path, examples=True)
    _marketing(paths, routing={"default_assignee": "mk-strategy", "handoffs": [
        {"from": "mk-drafting", "to": "mk-review", "when": "drafted"}]})
    create_task(paths.tasks, "write the launch post", assignee="mk-drafting",
                lane="drafting", metadata={"team_id": "mk"})

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "mk-drafting")

    assert [t.assignee for t in list_tasks(paths.tasks) if t.state == "pending"] == ["mk-review"]
    assert [e["type"] for e in _events(paths) if e["type"] == "team.handoff.fired"]


def test_an_engineering_team_still_stands_its_handoffs_down(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _write_team(paths, "eng", lanes=PIPELINE_LANES,
                routing={"default_assignee": "eng-lead", "handoffs": [
                    {"from": "eng-dev", "to": "eng-test", "when": "ready"}]},
                agents=[{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"},
                        {"id": "eng-test", "role": "test"}])
    create_task(paths.tasks, "ship it", assignee="eng-dev", lane="in-progress",
                metadata={"team_id": "eng"})

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    assert len(list_tasks(paths.tasks)) == 1         # one row, still
    # No spawned ticket, by either route: the bounced ticket never reaches the
    # completion side effects at all, and fire_handoffs stands itself down for
    # this team anyway (test_handoffs.py covers that skip and its audit event).
    assert not [e for e in _events(paths) if e["type"] == "team.handoff.fired"]
    ticket = list_tasks(paths.tasks)[0]
    assert (ticket.state, ticket.lane, ticket.assignee) == ("pending", "backlog", "eng-lead")


# --- the close lane ---------------------------------------------------------


def _close(paths, actor, ticket_id):
    cap = next(c for c in bundled_capabilities() if "tickets.close" in c.actions)
    agent = AgentConfig(id=actor, name=actor, role="r", memory_scope="task_only",
                        tools=["tickets.close"], permissions={})
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    return _tickets_handler(WorkflowStep(id="s", action="tickets.close", input={}),
                            cap, {"ticket": ticket_id}, {}, runtime)


def test_the_close_lane_comes_from_the_teams_own_rules(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    team = _write_team(
        paths, "eng",
        lanes=[{"id": "backlog"}, {"id": "building"}, {"id": "awaiting-merge"}, {"id": "done"}],
        transitions={"rules": [{"from": "lead", "to": "dev", "lane": "building"},
                               {"from": "dev", "to": "lead", "lane": "awaiting-merge"}],
                     "bounce_lane": "backlog"},
        agents=[{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"}])
    assert close_lane(team) == "awaiting-merge"
    assert is_lifecycle_managed(team)

    ticket = create_task(paths.tasks, "renamed board", assignee="eng-lead",
                         lane="awaiting-merge", metadata={"team_id": "eng"})
    _close(paths, "eng-lead", ticket.id)
    fresh = find_task(paths.tasks, ticket.id)
    assert (fresh.lane, fresh.state) == ("done", "completed")


def test_closing_from_the_wrong_lane_is_still_refused(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _write_team(
        paths, "eng",
        lanes=[{"id": "backlog"}, {"id": "building"}, {"id": "awaiting-merge"}, {"id": "done"}],
        transitions={"rules": [{"from": "lead", "to": "dev", "lane": "building"},
                               {"from": "dev", "to": "lead", "lane": "awaiting-merge"}],
                     "bounce_lane": "backlog"},
        agents=[{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"}])
    ticket = create_task(paths.tasks, "not ready", assignee="eng-lead", lane="building",
                         metadata={"team_id": "eng"})

    with pytest.raises(ValueError, match="awaiting-merge"):
        _close(paths, "eng-lead", ticket.id)
    assert find_task(paths.tasks, ticket.id).lane == "building"

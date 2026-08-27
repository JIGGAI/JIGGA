"""A lead that decomposes must not un-decompose the epic on its way out.

`tickets.decompose` parks the epic in `waiting` mid-run. The run that did it
then finishes holding that same ticket, so the outcome resolution saw a
lane-managed ticket its agent had not handed on and bounced it — overwriting
the `waiting` the decompose had just set.

Observed live, not hypothesised: a lead decomposed "Build a link shortener"
into three correctly-linked stories, and the board still showed the epic back
in `backlog` with `bounces: 1`. 1786 unit tests passed while this was broken,
because none of them decomposed DURING a run and then let the run end.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import TeamConfig
from jigga.runtime.agent import run_agent
from jigga.runtime.decompose import decompose
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.tasks import create_task, find_task
from jigga.runtime.ticket_outcome import resolve_ticket_outcome

PIPELINE = [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
            {"id": "ready-for-pr"}, {"id": "done"}]
TRANSITIONS = {
    "rules": [
        {"from": "lead", "to": "dev", "lane": "in-progress"},
        {"from": "dev", "to": "test", "lane": "testing"},
        {"from": "test", "to": "dev", "lane": "in-progress"},
        {"from": "test", "to": "lead", "lane": "ready-for-pr"},
    ],
    "bounce_lane": "backlog",
}
ROSTER = [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"},
          {"id": "eng-test", "role": "test"}]


def _team_dict():
    return {"id": "eng", "name": "Eng", "agents": ROSTER, "lanes": PIPELINE,
            "lane_transitions": TRANSITIONS}


def _setup(tmp_path: Path):
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.teams / "eng.yaml", _team_dict())
    for aid in ("eng-lead", "eng-dev"):
        write_yaml(paths.agents / f"{aid}.yaml", {
            "id": aid, "name": aid, "role": "r", "memory_scope": "task_only",
            "tools": [], "permissions": {}, "permission_mode": "autonomous"})
    return paths


def test_a_waiting_ticket_is_left_alone_by_outcome_resolution() -> None:
    """The decision itself: a parked epic is not a stalled one."""
    from jigga.core.models import Task

    epic = Task(id="task_e", title="epic", assignee="eng-lead", lane="in-progress",
                state="waiting", metadata={"team_id": "eng", "children": ["task_a"]})
    out = resolve_ticket_outcome(epic, TeamConfig.from_dict(_team_dict()),
                                 run_state="completed", ran_as="eng-lead")

    assert out["state"] == "waiting"
    assert out["bounced"] is False
    assert out["lane"] == "in-progress"


def test_the_run_that_decomposes_does_not_bounce_its_own_epic(tmp_path: Path) -> None:
    """End to end, through a real run: decompose mid-run, then let it finish."""
    paths = _setup(tmp_path)
    epic = create_task(paths.tasks, "Build a link shortener", description="Three pieces.",
                       assignee="eng-lead", lane="backlog", metadata={"team_id": "eng"})

    def _decompose_then_finish(home, logs_dir, request):
        # Stands in for the lead calling tickets.decompose during its run.
        if not (find_task(paths.tasks, epic.id).metadata or {}).get("children"):
            decompose(paths.tasks, paths.teams, ticket_id=epic.id, actor="eng-lead",
                      summary="Cut by layer.", plan="shared-context/plans/ls.md",
                      stories=[{"title": "storage", "description": "brief", "assignee": "eng-dev"},
                               {"title": "api", "description": "brief", "assignee": "eng-dev"}])
        return ModelCallResult(status="ok", provider="dry_run", model="m",
                               content="decomposed", dry_run=True, tool_calls=[])

    with patch("jigga.runtime.agent.call_model", _decompose_then_finish):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-lead")

    fresh = find_task(paths.tasks, epic.id)
    assert fresh.state == "waiting", "the run bounced the epic it had just parked"
    assert fresh.lane == "in-progress"
    assert (fresh.metadata or {}).get("bounces") in (None, 0)
    assert len(fresh.metadata["children"]) == 2

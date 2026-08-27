"""The board describes its own rules, and refuses the wrong verb.

A lead holding a ticket called task.assign twice to give the work to its dev.
Each call put a second ticket on the board and left the original bouncing. The
rule was already in its role string, its workspace plan and its assembled
prompt — instruction alone did not take. So the board now states the rules it
generates from its own transition table, and task.assign refuses the one case
that is always wrong.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig, TeamConfig, WorkflowStep
from jigga.runtime.capabilities import bundled_capabilities
from jigga.runtime.handlers import _team_orchestration_handler
from jigga.runtime.lanes import render_lanes
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.tasks import create_task, list_tasks

PIPELINE_TRANSITIONS = {
    "rules": [
        {"from": "lead", "to": "dev", "lane": "in-progress"},
        {"from": "dev", "to": "test", "lane": "testing"},
        {"from": "test", "to": "dev", "lane": "in-progress"},
        {"from": "test", "to": "lead", "lane": "ready-for-pr"},
    ],
    "bounce_lane": "backlog",
}

PIPELINE = [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
            {"id": "ready-for-pr"}, {"id": "done"}]
ROSTER = [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"},
          {"id": "eng-test", "role": "test"}]


def _team(lanes=None, agents=None) -> TeamConfig:
    board = lanes if lanes is not None else PIPELINE
    data = {"id": "eng", "name": "Eng",
            "agents": agents if agents is not None else ROSTER, "lanes": board}
    if any(x.get("id") == "ready-for-pr" for x in board):
        data["lane_transitions"] = PIPELINE_TRANSITIONS
    return TeamConfig.from_dict(data)


# --- the board states its own rules -----------------------------------------

def test_a_lifecycle_board_names_the_handoff_verb_and_the_alternative() -> None:
    text = render_lanes(_team())
    assert "tickets.handoff" in text
    assert "task.assign" in text, "naming only the right verb leaves the wrong one unmarked"
    assert "tickets.close" in text


def test_the_transitions_are_rendered_with_real_agent_ids() -> None:
    # Generated from the team's own table, so it cannot drift from the roster.
    text = render_lanes(_team())
    assert "eng-dev -> eng-test  lands in testing" in text
    assert "eng-test -> eng-dev  lands in in-progress" in text     # QA rejected
    assert "eng-test -> eng-lead  lands in ready-for-pr" in text   # QA passed


def test_a_non_lifecycle_board_is_told_no_rules_it_cannot_follow() -> None:
    marketing = _team(lanes=[{"id": "brief"}, {"id": "drafting"}, {"id": "published"}], agents=[])
    text = render_lanes(marketing)
    assert "brief" in text                  # still gets its lane vocabulary
    assert "tickets.handoff" not in text    # but not a workflow it has no rules for


def test_a_team_with_no_board_renders_nothing() -> None:
    no_board = TeamConfig.from_dict({"id": "x", "name": "X", "agents": ROSTER})
    assert render_lanes(no_board) == ""


# --- task.assign refuses a disguised handoff --------------------------------

def _setup(tmp_path: Path):
    paths = init_runtime(tmp_path)
    write_yaml(paths.home / "teams" / "eng.yaml",
               {"id": "eng", "name": "Eng", "agents": ROSTER, "lanes": PIPELINE,
                "lane_transitions": PIPELINE_TRANSITIONS})
    return paths


def _assign(paths, actor: str, held_task_id: str | None, payload: dict):
    agent = AgentConfig(id=actor, name=actor, role="r", memory_scope="task_only",
                        tools=["task.assign"], permissions={})
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions", task_id=held_task_id)
    cap = next(c for c in bundled_capabilities() if "task.assign" in c.actions)
    return _team_orchestration_handler(
        WorkflowStep(id="s", action="task.assign", input={}), cap, payload, {}, runtime)


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def test_passing_a_held_ticket_to_a_teammate_is_refused(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    held = create_task(paths.tasks, "the work", assignee="eng-lead", lane="backlog",
                       metadata={"team_id": "eng"})

    with pytest.raises(ValueError) as excinfo:
        _assign(paths, "eng-lead", held.id, {"assignee": "eng-dev", "title": "same work again"})

    assert "tickets.handoff" in str(excinfo.value), "the refusal must name what to do instead"
    assert held.id in str(excinfo.value)
    assert [t.id for t in list_tasks(paths.tasks)] == [held.id], "no duplicate may be created"
    assert "task.assign.refused" in [e["type"] for e in _events(paths)]


def test_filing_genuinely_new_work_still_works(tmp_path: Path) -> None:
    # The lead holds nothing; this is what task.assign is for.
    paths = _setup(tmp_path)
    result = _assign(paths, "eng-lead", None, {"assignee": "eng-dev", "title": "brand new work"})
    assert result["assigned"]
    assert len(list_tasks(paths.tasks)) == 1


def test_assigning_outside_the_board_is_untouched(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    held = create_task(paths.tasks, "the work", assignee="eng-lead", lane="backlog",
                       metadata={"team_id": "eng"})
    result = _assign(paths, "eng-lead", held.id, {"assignee": "someone-else", "title": "other work"})
    assert result["assigned"], "only teammates on the held ticket's board are refused"


def test_holding_a_plain_task_does_not_refuse_anything(tmp_path: Path) -> None:
    # No lane means no board means nothing to hand off.
    paths = _setup(tmp_path)
    held = create_task(paths.tasks, "plain", assignee="eng-lead")
    result = _assign(paths, "eng-lead", held.id, {"assignee": "eng-dev", "title": "new work"})
    assert result["assigned"]

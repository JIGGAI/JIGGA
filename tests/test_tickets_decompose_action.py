"""The action the lead actually calls.

The dispatch trap: _tickets_handler switches on payload["action"] (default
"move"), NOT step.action. A branch written as `if action == "decompose"` is
unreachable, because callers pass WorkflowStep(action="tickets.decompose") with
no "action" key in the payload. handoff and close both hit this; follow their
pattern.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.agent import _parameters_for
from jigga.runtime.capabilities import bundled_capabilities
from jigga.runtime.handlers import _tickets_handler
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.tasks import create_task, find_task, list_tasks

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


def _cap():
    return next(c for c in bundled_capabilities() if "tickets.decompose" in c.actions)


def _setup(tmp_path: Path):
    paths = init_runtime(tmp_path)
    write_yaml(paths.teams / "eng.yaml", {"id": "eng", "name": "Eng", "agents": ROSTER,
                                          "lanes": PIPELINE, "lane_transitions": TRANSITIONS})
    return paths


def _events(paths, event_type: str) -> list[dict]:
    path = paths.logs / "events.jsonl"
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [event for event in events if event["type"] == event_type]


def _act(paths, actor: str, payload: dict):
    agent = AgentConfig(id=actor, name=actor, role="r", memory_scope="task_only",
                        tools=["tickets.decompose"], permissions={})
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    return _tickets_handler(WorkflowStep(id="s", action="tickets.decompose", input={}),
                            _cap(), payload, {}, runtime)


def test_the_schema_names_every_field(tmp_path: Path) -> None:
    schema = _parameters_for("tickets.decompose", _cap())
    assert set(schema["properties"]) >= {"ticket", "summary", "plan", "stories"}
    assert set(schema.get("required", [])) >= {"ticket", "summary", "plan", "stories"}


def test_the_schema_declares_the_shape_of_a_story(tmp_path: Path) -> None:
    """An array with no `items` told the model nothing, so it invented an item
    shape — a list of bare title strings — and the call died on an
    AttributeError instead of being refused."""
    items = _parameters_for("tickets.decompose", _cap())["properties"]["stories"]["items"]
    assert items["type"] == "object"
    assert set(items["properties"]) == {"title", "description", "assignee"}
    assert set(items["required"]) == {"title", "description", "assignee"}


def test_the_capability_summary_advertises_decompose(tmp_path: Path) -> None:
    """summary + when_to_use IS the routing signal: a verb missing from the
    summary is a verb the model does not know it has."""
    assert "tickets.decompose" in _cap().summary


def test_the_lead_decomposes_through_the_action(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = create_task(paths.tasks, "New website", description="A website.",
                       assignee="eng-lead", lane="backlog", metadata={"team_id": "eng"})

    result = _act(paths, "eng-lead", {
        "ticket": epic.id, "summary": "Cut by surface.", "plan": "plans/x.md",
        "stories": [{"title": "Scaffold", "description": "brief", "assignee": "eng-dev"}]})

    assert len(result["stories"]) == 1
    assert find_task(paths.tasks, epic.id).state == "waiting"
    assert len(list_tasks(paths.tasks)) == 2


def test_a_refusal_surfaces_as_an_error_and_creates_nothing(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = create_task(paths.tasks, "New website", assignee="eng-lead", lane="backlog",
                       metadata={"team_id": "eng"})
    with pytest.raises(ValueError):
        _act(paths, "eng-dev", {"ticket": epic.id, "summary": "s", "plan": "p",
                                "stories": [{"title": "t", "description": "d",
                                             "assignee": "eng-dev"}]})
    assert len(list_tasks(paths.tasks)) == 1
    # An error raised at the agent is not a record. Every other guardrail on
    # this board is auditable, and a refusal nobody can find afterwards is the
    # invisible failure this feature is built to remove.
    refused = _events(paths, "ticket.decompose.refused")
    assert len(refused) == 1
    assert refused[0]["status"] == "deny"
    assert refused[0]["details"]["task_id"] == epic.id
    assert "lead" in refused[0]["details"]["reason"]


def test_a_list_of_bare_titles_is_refused_and_audited(tmp_path: Path) -> None:
    """The shape the untyped schema invited. It used to raise AttributeError
    inside decompose() — not a DecomposeError, so `except DecomposeError` missed
    it and NO refusal event was ever written."""
    from jigga.runtime.decompose import DecomposeError

    paths = _setup(tmp_path)
    epic = create_task(paths.tasks, "New website", assignee="eng-lead", lane="backlog",
                       metadata={"team_id": "eng"})

    with pytest.raises(DecomposeError) as caught:
        _act(paths, "eng-lead", {"ticket": epic.id, "summary": "s", "plan": "p",
                                 "stories": ["Scaffold the app", "Build the nav"]})

    assert "object" in str(caught.value)
    assert len(list_tasks(paths.tasks)) == 1
    assert find_task(paths.tasks, epic.id).state != "waiting"
    refused = _events(paths, "ticket.decompose.refused")
    assert len(refused) == 1
    assert refused[0]["details"]["task_id"] == epic.id
    assert "Scaffold the app" in refused[0]["details"]["reason"]


def test_it_requires_its_arguments(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    with pytest.raises(ValueError):
        _act(paths, "eng-lead", {"ticket": "task_x"})

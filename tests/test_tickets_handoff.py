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
from jigga.runtime.tasks import create_task, find_task


def _cap():
    return next(c for c in bundled_capabilities() if "tickets.handoff" in c.actions)


def _setup(tmp_path: Path):
    paths = init_runtime(tmp_path)
    write_yaml(paths.home / "teams" / "eng.yaml", {
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"},
                   {"id": "eng-test", "role": "test"}],
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
                  {"id": "ready-for-pr"}, {"id": "done"}],
    })
    return paths


def _runtime(paths, agent_id: str) -> RuntimeContext:
    agent = AgentConfig(id=agent_id, name=agent_id, role="r", memory_scope="task_only",
                        tools=["tickets.handoff"], permissions={})
    return RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                          sessions_dir=paths.home / "sessions")


def _handoff(paths, actor, payload):
    return _tickets_handler(WorkflowStep(id="s", action="tickets.handoff", input={}),
                            _cap(), payload, {}, _runtime(paths, actor))


def test_the_schema_names_the_real_fields() -> None:
    schema = _parameters_for("tickets.handoff", _cap())
    assert set(schema["properties"]) >= {"ticket", "assignee", "comment"}
    assert set(schema.get("required", [])) >= {"ticket", "assignee"}


def test_handoff_moves_assignee_lane_and_state_together(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "build", assignee="eng-dev", lane="in-progress",
                    metadata={"team_id": "eng"})

    _handoff(paths, "eng-dev", {"ticket": t.id, "assignee": "eng-test"})

    fresh = find_task(paths.tasks, t.id)
    assert fresh.assignee == "eng-test"
    assert fresh.lane == "testing"
    assert fresh.state == "pending"


def test_no_second_ticket_is_created(tmp_path: Path) -> None:
    # The whole point: one ticket travels the board.
    from jigga.runtime.tasks import list_tasks
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "build", assignee="eng-dev", lane="in-progress",
                    metadata={"team_id": "eng"})
    _handoff(paths, "eng-dev", {"ticket": t.id, "assignee": "eng-test"})
    assert [x.id for x in list_tasks(paths.tasks)] == [t.id]


def test_an_underived_transition_keeps_the_lane_and_says_so(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "build", assignee="eng-dev", lane="in-progress",
                    metadata={"team_id": "eng"})

    result = _handoff(paths, "eng-dev", {"ticket": t.id, "assignee": "eng-lead"})

    assert find_task(paths.tasks, t.id).lane == "in-progress"
    assert result["lane_derived"] is False
    events = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines() if line.strip()]
    assert "ticket.lane.underived" in [e["type"] for e in events]


def test_handoff_requires_a_ticket_and_an_assignee(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    with pytest.raises(ValueError):
        _handoff(paths, "eng-dev", {"ticket": "task_x"})

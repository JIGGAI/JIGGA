"""One ticket, one row, walking backlog -> done.

Before this, the lead spawned a child ticket, the dev spawned handoff tickets,
and each agent's run marked its own row completed. One request produced four
tickets and three false completions.
"""
from __future__ import annotations

from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.capabilities import bundled_capabilities
from jigga.runtime.handlers import _tickets_handler
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.tasks import create_task, find_task, list_tasks


def _cap(action: str):
    return next(c for c in bundled_capabilities() if action in c.actions)


def _act(paths, actor: str, action: str, payload: dict):
    agent = AgentConfig(id=actor, name=actor, role="r", memory_scope="task_only",
                        tools=[action], permissions={})
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    return _tickets_handler(WorkflowStep(id="s", action=action, input={}),
                            _cap(action), payload, {}, runtime)


def test_one_ticket_walks_the_whole_board(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    write_yaml(paths.home / "teams" / "eng.yaml", {
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"},
                   {"id": "eng-test", "role": "test"}],
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
                  {"id": "ready-for-pr"}, {"id": "done"}],
    })
    ticket = create_task(paths.tasks, "New website", assignee="eng-lead", lane="backlog",
                         metadata={"team_id": "eng"})

    _act(paths, "eng-lead", "tickets.handoff", {"ticket": ticket.id, "assignee": "eng-dev"})
    assert find_task(paths.tasks, ticket.id).lane == "in-progress"

    _act(paths, "eng-dev", "tickets.handoff", {"ticket": ticket.id, "assignee": "eng-test"})
    assert find_task(paths.tasks, ticket.id).lane == "testing"

    _act(paths, "eng-test", "tickets.handoff", {"ticket": ticket.id, "assignee": "eng-lead"})
    assert find_task(paths.tasks, ticket.id).lane == "ready-for-pr"

    _act(paths, "eng-lead", "tickets.close", {"ticket": ticket.id})
    final = find_task(paths.tasks, ticket.id)
    assert final.lane == "done"
    assert final.state == "completed"

    # One row for one piece of work.
    assert [t.id for t in list_tasks(paths.tasks)] == [ticket.id]


def test_qa_can_send_it_back(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    write_yaml(paths.home / "teams" / "eng.yaml", {
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-dev", "role": "dev"}, {"id": "eng-test", "role": "test"}],
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"}, {"id": "done"}],
    })
    ticket = create_task(paths.tasks, "broken", assignee="eng-test", lane="testing",
                         metadata={"team_id": "eng"})

    _act(paths, "eng-test", "tickets.handoff", {"ticket": ticket.id, "assignee": "eng-dev"})

    fresh = find_task(paths.tasks, ticket.id)
    assert fresh.lane == "in-progress"     # rejection is a real transition, not a bounce
    assert fresh.assignee == "eng-dev"

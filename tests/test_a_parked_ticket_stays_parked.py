"""A run must not undo what its own tool calls decided.

The epic case shipped in PR #242 and the blocked case is its twin, found five
minutes after the loop guard went live: the guard blocked a looping ticket,
logged it, and then the same run's outcome resolution un-blocked it and bounced
it to `backlog`. The loop resumed with a laundered ticket — the guard fired,
said so in the audit log, and appeared not to work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig, Task, TeamConfig, WorkflowStep
from jigga.runtime.capabilities import bundled_capabilities
from jigga.runtime.handlers import _tickets_handler
from jigga.runtime.handoff_loop import MAX_PAIR_HANDOFFS
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.tasks import create_task, find_task
from jigga.runtime.ticket_outcome import PARKED_STATES, resolve_ticket_outcome

TRANSITIONS = {
    "rules": [
        {"from": "lead", "to": "dev", "lane": "in-progress"},
        {"from": "dev", "to": "lead", "lane": "ready-for-pr"},
    ],
    "bounce_lane": "backlog",
}

TEAM = TeamConfig.from_dict({
    "id": "eng", "name": "Eng",
    "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"}],
    "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "ready-for-pr"}, {"id": "done"}],
    "lane_transitions": TRANSITIONS,
})


@pytest.mark.parametrize("parked", sorted(PARKED_STATES))
def test_a_parked_ticket_survives_the_run_that_parked_it(parked: str) -> None:
    task = Task(id="t1", title="epic", state=parked, lane="in-progress",
                assignee="eng-lead", metadata={"team_id": "eng"})

    outcome = resolve_ticket_outcome(task, TEAM, run_state="completed", ran_as="eng-lead")

    assert outcome["state"] == parked, "the run's state overwrote the ticket's"
    assert outcome["bounced"] is False
    assert outcome["lane"] == "in-progress"
    assert outcome["assignee"] == "eng-lead"


def test_an_ordinary_finished_ticket_still_bounces() -> None:
    # The guard must not swallow the bounce it sits in front of.
    task = Task(id="t2", title="work", state="running", lane="in-progress",
                assignee="eng-dev", metadata={"team_id": "eng"})

    outcome = resolve_ticket_outcome(task, TEAM, run_state="completed", ran_as="eng-dev")

    assert outcome["bounced"] is True
    assert outcome["lane"] == "backlog"
    assert outcome["assignee"] == "eng-lead"


# --- the two guards together, through the real handler --------------------


def _setup(tmp_path: Path):
    paths = init_runtime(tmp_path)
    write_yaml(paths.home / "teams" / "eng.yaml", {
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"}],
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "ready-for-pr"},
                  {"id": "done"}],
        "lane_transitions": TRANSITIONS,
    })
    return paths


def _handoff(paths, actor, payload):
    agent = AgentConfig(id=actor, name=actor, role="r", memory_scope="task_only",
                        tools=["tickets.handoff"], permissions={})
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    return _tickets_handler(WorkflowStep(id="s", action="tickets.handoff", input={}),
                            next(c for c in bundled_capabilities() if "tickets.handoff" in c.actions),
                            payload, {}, runtime)


def test_the_loop_guards_block_is_not_undone_by_the_same_run(tmp_path: Path) -> None:
    # Reproduces the live sequence: lead and dev ping-pong past the limit, the
    # guard blocks the ticket, and then the lead's run ends holding it.
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "verify", assignee="eng-lead", lane="in-progress",
                    metadata={"team_id": "eng"})

    for i in range(MAX_PAIR_HANDOFFS):
        actor, other = ("eng-lead", "eng-dev") if i % 2 == 0 else ("eng-dev", "eng-lead")
        _handoff(paths, actor, {"ticket": t.id, "assignee": other})

    blocked = find_task(paths.tasks, t.id)
    assert blocked.state == "blocked"

    # The run that blocked it now finishes.
    outcome = resolve_ticket_outcome(blocked, TEAM, run_state="completed", ran_as="eng-lead")

    assert outcome["state"] == "blocked"
    assert outcome["bounced"] is False, "the block was laundered and the loop would resume"


def test_a_blocked_ticket_is_not_quietly_reassigned() -> None:
    # Bouncing sets a new assignee and lane. A blocked ticket keeps both, so
    # the board still shows who was holding it when it stopped.
    task = Task(id="t3", title="verify", state="blocked", lane="in-progress",
                assignee="eng-dev", metadata={"team_id": "eng", "bounces": 0})

    outcome = resolve_ticket_outcome(task, TEAM, run_state="completed", ran_as="eng-dev")

    assert outcome["assignee"] == "eng-dev"
    assert outcome["lane"] == "in-progress"

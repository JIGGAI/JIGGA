# Ticket Lane Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a team ticket's lane its lifecycle — one ticket travels the board, the runtime moves it, and `completed` is reachable only from `done`.

**Architecture:** `Task.lane` becomes the ticket lifecycle while `Task.state` goes back to describing the current run. A new `tickets.handoff` action reassigns the existing ticket and lets the runtime derive the destination lane from the transition (who handed it to whom), so agents cannot leave the board disagreeing with who holds the work. `tickets.close` is the only path to `completed`. Unhandled tickets bounce to the lead with a bounded counter.

**Tech Stack:** Python 3.11+, pytest, ruff. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-26-ticket-lane-lifecycle-design.md`

## Global Constraints

- **Work on the remote box.** All paths are on `control@100.103.210.102`. Repo `~/JIGGA`; venv `source ~/JIGGA/.venv/bin/activate`. `~/jigga-stable` is production and is NOT touched by this plan.
- **`ruff check .` before every commit.** CI lints before it tests, so a lint error fails all three matrix legs without running a single test.
- **Non-team tasks must not change.** Anything with `lane is None` keeps today's behaviour exactly. Every task in this plan is gated on the ticket being lane-managed.
- **Only `tickets.close` may write `completed`** for a lane-managed ticket. No other code path.
- **Deny/gate rules still win.** `move_task_lane` already enforces lane gates; nothing here bypasses it.
- **Branch:** `feat/ticket-lane-lifecycle` (already exists, holds the spec).
- **Every new capability action declares `action_inputs`.** An action advertised as an open object gets its arguments guessed and silently dropped.

---

### Task 1: Lane transition table and derivation

**Files:**
- Modify: `jigga/runtime/lanes.py` (append after `find_lane`, ~line 111)
- Test: `tests/test_lane_transitions.py` (create)

**Interfaces:**
- Consumes: `TeamConfig`, `team_lanes(team)`, `find_lane(team, lane_id)` from `lanes.py`
- Produces:
  - `role_of(team: TeamConfig, agent_id: str) -> str | None`
  - `lane_transitions(team: TeamConfig) -> dict` → `{"rules": list[dict], "bounce_lane": str | None}`
  - `derive_lane(team: TeamConfig, from_agent: str | None, to_agent: str) -> str | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lane_transitions.py
from __future__ import annotations

from jigga.core.models import TeamConfig
from jigga.runtime.lanes import derive_lane, lane_transitions, role_of

ENG_LANES = [
    {"id": "backlog"}, {"id": "in-progress"},
    {"id": "testing", "gate": "test"},
    {"id": "ready-for-pr", "gate": "lead"},
    {"id": "done"},
]
AGENTS = [
    {"id": "eng-lead", "role": "lead"},
    {"id": "eng-dev", "role": "dev"},
    {"id": "eng-test", "role": "test"},
]


def _team(lanes=None, transitions=None) -> TeamConfig:
    data = {"id": "eng", "name": "Eng", "agents": AGENTS, "lanes": lanes or ENG_LANES}
    if transitions is not None:
        data["lane_transitions"] = transitions
    return TeamConfig.from_dict(data)


def test_role_of_reads_the_team_roster() -> None:
    team = _team()
    assert role_of(team, "eng-dev") == "dev"
    assert role_of(team, "nobody") is None


def test_default_transitions_cover_the_standard_pipeline() -> None:
    team = _team()
    assert derive_lane(team, "eng-lead", "eng-dev") == "in-progress"
    assert derive_lane(team, "eng-dev", "eng-test") == "testing"
    assert derive_lane(team, "eng-test", "eng-lead") == "ready-for-pr"


def test_qa_rejection_returns_the_ticket_to_the_author() -> None:
    # A rejected ticket must go back to in-progress, not fall through to the
    # bounce lane, which would lose the fact that QA actively sent it back.
    team = _team()
    assert derive_lane(team, "eng-test", "eng-dev") == "in-progress"


def test_an_undeclared_transition_derives_nothing() -> None:
    team = _team()
    assert derive_lane(team, "eng-dev", "eng-lead") is None


def test_a_team_may_declare_its_own_rules() -> None:
    team = _team(transitions={"rules": [{"from": "lead", "to": "dev", "lane": "testing"}],
                              "bounce_lane": "backlog"})
    assert derive_lane(team, "eng-lead", "eng-dev") == "testing"
    assert derive_lane(team, "eng-dev", "eng-test") is None   # custom table replaces defaults
    assert lane_transitions(team)["bounce_lane"] == "backlog"


def test_defaults_are_dropped_when_the_lane_does_not_exist() -> None:
    # A team whose board has no `testing` lane must not be handed one.
    team = _team(lanes=[{"id": "backlog"}, {"id": "in-progress"}, {"id": "done"}])
    assert derive_lane(team, "eng-dev", "eng-test") is None
    assert derive_lane(team, "eng-lead", "eng-dev") == "in-progress"


def test_bounce_lane_defaults_to_backlog_when_present() -> None:
    assert lane_transitions(_team())["bounce_lane"] == "backlog"
    assert lane_transitions(_team(lanes=[{"id": "a"}, {"id": "done"}]))["bounce_lane"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/JIGGA && source .venv/bin/activate && python -m pytest tests/test_lane_transitions.py -v`
Expected: FAIL — `ImportError: cannot import name 'derive_lane' from 'jigga.runtime.lanes'`

- [ ] **Step 3: Write minimal implementation**

Append to `jigga/runtime/lanes.py`:

```python
# --- lane transitions -------------------------------------------------------
#
# The destination lane is derived from the TRANSITION (who handed the ticket to
# whom), not from the target role alone, because a role can own several lanes:
# the lead owns `backlog` (work bounced back), `ready-for-pr` (QA passed) and
# `done`. "Assign to lead" therefore does not identify a lane; "test handed it
# to lead" does.

DEFAULT_LANE_TRANSITIONS: list[dict[str, str]] = [
    {"from": "lead", "to": "dev", "lane": "in-progress"},
    {"from": "dev", "to": "test", "lane": "testing"},
    {"from": "test", "to": "dev", "lane": "in-progress"},    # QA rejected
    {"from": "test", "to": "lead", "lane": "ready-for-pr"},  # QA passed
]
DEFAULT_BOUNCE_LANE = "backlog"


def role_of(team: TeamConfig, agent_id: str) -> str | None:
    """The team role (`dev`, `test`, `lead`, ...) for an agent id."""
    for member in team.agents or []:
        if isinstance(member, dict) and member.get("id") == agent_id:
            role = member.get("role")
            return str(role) if role else None
    return None


def lane_transitions(team: TeamConfig) -> dict[str, Any]:
    """The team's transition table, defaulted to the standard pipeline.

    Defaults are filtered against the team's actual lanes — a board without a
    `testing` lane must not be handed one — and a team that declares its own
    `rules` replaces the defaults outright rather than merging, so a custom
    board is exactly what it says it is.
    """
    known = {lane.id for lane in team_lanes(team)}
    declared = getattr(team, "lane_transitions", None)
    declared = declared if isinstance(declared, dict) else {}

    rules = declared.get("rules")
    if not isinstance(rules, list):
        rules = DEFAULT_LANE_TRANSITIONS
    rules = [r for r in rules if isinstance(r, dict) and r.get("lane") in known]

    bounce = declared.get("bounce_lane", DEFAULT_BOUNCE_LANE)
    if bounce not in known:
        bounce = None
    return {"rules": rules, "bounce_lane": bounce}


def derive_lane(team: TeamConfig, from_agent: str | None, to_agent: str) -> str | None:
    """The lane a handoff moves the ticket into, or None when no rule matches.

    None is not an error — the caller leaves the lane alone and says so, rather
    than guessing a destination.
    """
    from_role = role_of(team, from_agent) if from_agent else None
    to_role = role_of(team, to_agent)
    if not to_role:
        return None
    for rule in lane_transitions(team)["rules"]:
        if rule.get("to") == to_role and (rule.get("from") in (None, from_role)):
            return str(rule["lane"])
    return None
```

Add `lane_transitions: Any = None` to `TeamConfig` in `jigga/core/models.py`, directly under the existing `lanes` field, with this comment:

```python
    # Board transition table (raw passthrough; normalized by runtime/lanes.py).
    # None = the standard pipeline, filtered to the lanes this team declares.
    lane_transitions: Any = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_lane_transitions.py -v`
Expected: 7 passed

- [ ] **Step 5: Lint and commit**

```bash
cd ~/JIGGA && source .venv/bin/activate
ruff check . && python -m pytest tests/test_lane_transitions.py -q
git add jigga/runtime/lanes.py jigga/core/models.py tests/test_lane_transitions.py
git commit -m "Derive a ticket's lane from the handoff transition

A role can own several lanes -- the lead owns backlog, ready-for-pr and
done -- so the target role alone cannot identify a destination. The
transition can: test handing to lead means ready-for-pr, test handing
back to dev means QA rejected it.

Defaults are filtered against the team's declared lanes so a board
without a testing lane is never handed one, and a team that declares its
own rules replaces the defaults outright."
```

---

### Task 2: Ticket outcome resolution — only `done` completes

**Files:**
- Create: `jigga/runtime/ticket_outcome.py`
- Test: `tests/test_ticket_outcome.py` (create)

**Interfaces:**
- Consumes: `role_of`, `lane_transitions` from Task 1; `Task` from `jigga.core.models`
- Produces: `resolve_ticket_outcome(task, team, run_state) -> TicketOutcome` where
  `TicketOutcome = {"state": str, "lane": str | None, "assignee": str | None, "bounced": bool}`

This is a pure function so the decision can be tested without running an agent. Task 3 wires it in.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ticket_outcome.py
from __future__ import annotations

from jigga.core.models import Task, TeamConfig
from jigga.runtime.ticket_outcome import resolve_ticket_outcome

TEAM = TeamConfig.from_dict({
    "id": "eng", "name": "Eng",
    "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"}],
    "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
              {"id": "ready-for-pr"}, {"id": "done"}],
})


def _ticket(**kw) -> Task:
    base = {"id": "task_1", "title": "t", "assignee": "eng-dev", "lane": "in-progress"}
    base.update(kw)
    return Task(**base)


def test_a_finished_run_does_not_complete_the_ticket() -> None:
    # The whole point: finishing a run is not finishing the work.
    out = resolve_ticket_outcome(_ticket(), TEAM, run_state="completed")
    assert out["state"] != "completed"


def test_an_unhandled_ticket_bounces_to_the_lead() -> None:
    out = resolve_ticket_outcome(_ticket(), TEAM, run_state="completed")
    assert out == {"state": "pending", "lane": "backlog", "assignee": "eng-lead", "bounced": True}


def test_a_ticket_in_done_completes() -> None:
    out = resolve_ticket_outcome(_ticket(lane="done"), TEAM, run_state="completed")
    assert out["state"] == "completed"
    assert out["bounced"] is False


def test_a_failed_run_fails_the_ticket_and_moves_nothing() -> None:
    out = resolve_ticket_outcome(_ticket(), TEAM, run_state="failed")
    assert out == {"state": "failed", "lane": "in-progress", "assignee": "eng-dev", "bounced": False}


def test_an_approval_park_is_left_alone() -> None:
    out = resolve_ticket_outcome(_ticket(), TEAM, run_state="needs_approval")
    assert out["state"] == "needs_approval"
    assert out["bounced"] is False


def test_a_reassigned_ticket_is_not_a_bounce() -> None:
    # The agent handed it on during the run; the ticket already moved.
    ticket = _ticket(assignee="eng-test", lane="testing")
    out = resolve_ticket_outcome(ticket, TEAM, run_state="completed", ran_as="eng-dev")
    assert out == {"state": "pending", "lane": "testing", "assignee": "eng-test", "bounced": False}


def test_the_bounce_guard_blocks_after_three() -> None:
    ticket = _ticket(metadata={"bounces": 3})
    out = resolve_ticket_outcome(ticket, TEAM, run_state="completed")
    assert out["state"] == "blocked"


def test_a_team_without_a_bounce_lane_blocks_instead() -> None:
    team = TeamConfig.from_dict({"id": "x", "name": "X", "agents": [],
                                 "lanes": [{"id": "a"}, {"id": "done"}]})
    out = resolve_ticket_outcome(_ticket(lane="a"), team, run_state="completed")
    assert out["state"] == "blocked"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ticket_outcome.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jigga.runtime.ticket_outcome'`

- [ ] **Step 3: Write minimal implementation**

```python
# jigga/runtime/ticket_outcome.py
"""What a finished run means for the ticket it worked.

Finishing a run is not finishing the work. Before this, the end of a successful
run wrote `completed` onto the task, so a ticket the dev had merely handed to QA
read as done — and on 2026-08-25 two tickets read `completed` having produced
nothing at all. A ticket is complete when it reaches the `done` lane and at no
other moment.

Pure function: it decides, the caller writes. That keeps the rule testable
without standing up an agent run.
"""
from __future__ import annotations

from typing import TypedDict

from jigga.core.models import Task, TeamConfig
from jigga.runtime.lanes import lane_transitions

# How many times a ticket may return to the lead unhandled before it stops.
# Bouncing is how unowned work finds an owner, but a lead that reassigns
# blindly would ping-pong forever; this bounds it loudly instead.
MAX_BOUNCES = 3

DONE_LANE = "done"


class TicketOutcome(TypedDict):
    state: str
    lane: str | None
    assignee: str | None
    bounced: bool


def _lead_of(team: TeamConfig) -> str | None:
    for member in team.agents or []:
        if isinstance(member, dict) and member.get("role") == "lead":
            return str(member.get("id")) if member.get("id") else None
    return None


def resolve_ticket_outcome(
    task: Task, team: TeamConfig, *, run_state: str, ran_as: str | None = None,
) -> TicketOutcome:
    """Decide the ticket's assignee/lane/state after a run.

    `ran_as` is the agent whose run just ended. When the ticket's assignee is
    someone else, the agent handed it on mid-run and there is nothing to bounce.
    """
    keep: TicketOutcome = {"state": run_state, "lane": task.lane,
                           "assignee": task.assignee, "bounced": False}

    # A failed or parked run leaves the board untouched — the work is still
    # where it was, and the reason is on the run record.
    if run_state != "completed":
        return keep

    if task.lane == DONE_LANE:
        return {**keep, "state": "completed"}

    # Handed on during the run: the ticket already moved, so re-queue it for
    # whoever holds it now.
    if ran_as is not None and task.assignee != ran_as:
        return {**keep, "state": "pending"}

    # Nobody has it next. Bounce it to the lead so it lands somewhere visible
    # rather than sitting silently assigned to an agent that is finished with it.
    bounces = int((task.metadata or {}).get("bounces") or 0)
    bounce_lane = lane_transitions(team)["bounce_lane"]
    lead = _lead_of(team)
    if bounces >= MAX_BOUNCES or not bounce_lane or not lead:
        return {**keep, "state": "blocked"}
    return {"state": "pending", "lane": bounce_lane, "assignee": lead, "bounced": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ticket_outcome.py -v`
Expected: 8 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check . && python -m pytest tests/test_ticket_outcome.py -q
git add jigga/runtime/ticket_outcome.py tests/test_ticket_outcome.py
git commit -m "Decide a ticket's outcome from its lane, not its run

Finishing a run is not finishing the work. A ticket is complete when it
reaches the done lane and at no other moment; a run that ends without
handing the ticket on bounces it to the lead, bounded at three so a
lead that reassigns blindly cannot ping-pong forever.

Pure function so the rule is testable without standing up an agent."
```

---

### Task 3: Wire the outcome into the agent run

**Files:**
- Modify: `jigga/runtime/agent.py` (the `set_task_state(tasks_dir, task.id, loop["state"])` call, ~line 579)
- Test: `tests/test_ticket_lifecycle_integration.py` (create)

**Interfaces:**
- Consumes: `resolve_ticket_outcome` (Task 2), `update_task` from `jigga.runtime.tasks`
- Produces: nothing new; changes behaviour of `run_agent` for lane-managed tickets

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ticket_lifecycle_integration.py
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.agent import run_agent
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.tasks import create_task, list_tasks


def _result(content="done") -> ModelCallResult:
    return ModelCallResult(status="ok", provider="dry_run", model="m",
                           content=content, dry_run=True, tool_calls=[])


def _team(paths) -> None:
    write_yaml(paths.home / "teams" / "eng.yaml", {
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-lead", "role": "lead"}, {"id": "eng-dev", "role": "dev"}],
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
                  {"id": "ready-for-pr"}, {"id": "done"}],
    })
    for aid in ("eng-lead", "eng-dev"):
        write_yaml(paths.agents / f"{aid}.yaml", {
            "id": aid, "name": aid, "role": "r", "memory_scope": "task_only",
            "tools": [], "permissions": {}, "permission_mode": "autonomous"})


def test_a_finished_run_leaves_a_team_ticket_incomplete(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    create_task(paths.tasks, "ship it", assignee="eng-dev", lane="in-progress",
                metadata={"team_id": "eng"})

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    ticket = list_tasks(paths.tasks)[0]
    assert ticket.state != "completed"          # the run ended; the work did not
    assert ticket.assignee == "eng-lead"        # bounced to the lead
    assert ticket.lane == "backlog"
    assert ticket.metadata["bounces"] == 1


def test_a_ticket_in_done_completes(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    create_task(paths.tasks, "shipped", assignee="eng-dev", lane="done",
                metadata={"team_id": "eng"})

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    assert list_tasks(paths.tasks)[0].state == "completed"


def test_a_plain_task_is_untouched_by_any_of_this(tmp_path: Path) -> None:
    # No lane means no board; today's behaviour must survive exactly.
    paths = init_runtime(tmp_path, examples=True)
    _team(paths)
    create_task(paths.tasks, "plain", assignee="eng-dev")

    with patch("jigga.runtime.agent.call_model", lambda *a, **k: _result()):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    ticket = list_tasks(paths.tasks)[0]
    assert ticket.state == "completed"
    assert ticket.assignee == "eng-dev"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ticket_lifecycle_integration.py -v`
Expected: FAIL on the first test — `assert 'completed' != 'completed'`

- [ ] **Step 3: Write minimal implementation**

In `jigga/runtime/agent.py`, replace:

```python
        completed = set_task_state(tasks_dir, task.id, loop["state"])
        processed.append(completed.to_dict())
```

with:

```python
        completed = _apply_ticket_outcome(home, tasks_dir, logs_dir, task, agent_id, loop["state"])
        processed.append(completed.to_dict())
```

and add this helper above `_run_agent`:

```python
def _apply_ticket_outcome(home: Path, tasks_dir: Path, logs_dir: Path, task,
                          agent_id: str, run_state: str):
    """Write the run's outcome onto the task.

    For a plain task that is just the run state, exactly as before. For a
    lane-managed ticket the lane decides: only `done` completes, and a ticket
    nobody picked up bounces to the lead instead of sitting silently assigned
    to an agent that has finished with it.
    """
    from jigga.core.config import load_teams
    from jigga.runtime.ticket_outcome import resolve_ticket_outcome

    fresh = find_task(tasks_dir, task.id) or task      # the run may have moved it
    team_id = (fresh.metadata or {}).get("team_id")
    team = load_teams(home / "teams").get(team_id) if team_id else None
    if fresh.lane is None or team is None:
        return set_task_state(tasks_dir, task.id, run_state)

    outcome = resolve_ticket_outcome(fresh, team, run_state=run_state, ran_as=agent_id)
    metadata = dict(fresh.metadata or {})
    if outcome["bounced"]:
        metadata["bounces"] = int(metadata.get("bounces") or 0) + 1
        append_event(logs_dir, "ticket.bounced", status="ask", agent=agent_id, task_id=fresh.id,
                     to=outcome["assignee"], lane=outcome["lane"], bounces=metadata["bounces"])
    if outcome["state"] == "blocked":
        append_event(logs_dir, "ticket.blocked", status="ask", agent=agent_id, task_id=fresh.id,
                     reason="nobody picked this ticket up and it has bounced too often")
    return update_task(tasks_dir, task.id, state=outcome["state"], lane=outcome["lane"],
                       assignee=outcome["assignee"], metadata=metadata)
```

Add `find_task` and `update_task` to the existing `jigga.runtime.tasks` import at the top of `agent.py`.

Read `jigga/runtime/tasks.py` for `update_task`'s signature before writing this — it uses an `_UNSET` sentinel so an omitted field means "leave alone". If it does not accept `state`, `lane`, `assignee` and `metadata` together, add the missing keyword arguments following the existing `_UNSET` pattern, and cover that with a unit test in `tests/test_tasks.py` before using it here.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ticket_lifecycle_integration.py -v && python -m pytest -q`
Expected: 3 passed, then the full suite green. Existing tests that assert `completed` on a *team* ticket must be updated — read each one and confirm it is asserting the old rule before changing it.

- [ ] **Step 5: Lint and commit**

```bash
ruff check . && python -m pytest -q
git add jigga/runtime/agent.py jigga/runtime/tasks.py tests/
git commit -m "A finished run no longer completes a team ticket

The end of a run wrote completed onto the task, so a ticket the dev had
merely handed to QA read as done. Lane-managed tickets now take their
state from resolve_ticket_outcome: only done completes, and a ticket
nobody picked up bounces to the lead. Plain tasks are untouched."
```

---

### Task 4: `tickets.handoff`

**Files:**
- Modify: `jigga/runtime/handlers.py` (`_tickets_handler`, ~line 340)
- Modify: `jigga/runtime/capabilities.py` (the `tickets` capability block)
- Test: `tests/test_tickets_handoff.py` (create)

**Interfaces:**
- Consumes: `derive_lane` (Task 1), `move_task_lane`, `update_task`
- Produces: action `tickets.handoff` with input `{ticket, assignee, comment?}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tickets_handoff.py
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
    events = [json.loads(l) for l in (paths.logs / "events.jsonl").read_text().splitlines() if l.strip()]
    assert "ticket.lane.underived" in [e["type"] for e in events]


def test_handoff_requires_a_ticket_and_an_assignee(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    with pytest.raises(ValueError):
        _handoff(paths, "eng-dev", {"ticket": "task_x"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tickets_handoff.py -v`
Expected: FAIL — `StopIteration` in `_cap()`, because no bundled capability declares `tickets.handoff`

- [ ] **Step 3: Write minimal implementation**

In `jigga/runtime/capabilities.py`, add `"tickets.handoff"` to the `tickets` capability's `actions` list and add its `action_inputs` (create the `action_inputs` key if the block has none):

```python
        "action_inputs": {
            "tickets.handoff": {
                "ticket": {"type": "string", "required": True,
                           "description": "Id of the EXISTING ticket to hand on. Do not create a new one."},
                "assignee": {"type": "string", "required": True,
                             "description": "Agent id taking the ticket next."},
                "comment": {"type": "string",
                            "description": "What you did and what the next agent needs to know."},
            },
        },
```

In `jigga/runtime/handlers.py`, inside `_tickets_handler`, before the `tickets.move` branch:

```python
    if action == "handoff":
        from jigga.runtime.lanes import derive_lane, team_for_task
        from jigga.runtime.tasks import find_task, update_task

        task_id = str(payload.get("ticket") or payload.get("task") or "").strip()
        assignee = str(payload.get("assignee") or "").strip()
        if not task_id or not assignee:
            raise ValueError("tickets.handoff needs a 'ticket' id and an 'assignee'.")
        task = find_task(tasks_dir, task_id)
        if task is None:
            raise ValueError(f"Ticket not found: {task_id}")

        _team_id, team = team_for_task(teams_dir, task)
        lane = derive_lane(team, actor, assignee)
        if lane is None:
            # No rule covers this transition. Leave the lane where it is and say
            # so — guessing a destination would put the board somewhere nobody
            # asked for, and silence is what made earlier losses invisible.
            append_event(runtime.logs_dir, "ticket.lane.underived", status="ask",
                         agent=actor, task_id=task.id, to=assignee, lane=task.lane)
        updated = update_task(tasks_dir, task_id, assignee=assignee, state="pending",
                              **({"lane": lane} if lane else {}))
        append_event(runtime.logs_dir, "team.ticket.handoff", agent=actor, task_id=task.id,
                     to=assignee, lane=updated.lane)
        return {"source": "capability.tickets", "ticket": task.id, "assignee": assignee,
                "lane": updated.lane, "lane_derived": lane is not None}
```

Add `from jigga.runtime.audit import append_event` to the handler's imports if it is not already there.

Note: this writes the lane directly rather than through `move_task_lane`, because a handoff is not an agent moving a ticket out of a gated lane on its own authority — it is the runtime recording where the work now sits. Gate enforcement stays on `tickets.move`, which is unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tickets_handoff.py -v`
Expected: 5 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check . && python -m pytest -q
git add jigga/runtime/handlers.py jigga/runtime/capabilities.py tests/test_tickets_handoff.py
git commit -m "Add tickets.handoff so one ticket travels the board

Handing work on reassigns the existing ticket and lets the runtime
derive the destination lane from the transition, instead of spawning a
second ticket for the same work. An undeclared transition leaves the
lane alone and emits ticket.lane.underived rather than guessing."
```

---

### Task 5: `tickets.close` — the only path to `completed`

**Files:**
- Modify: `jigga/runtime/handlers.py` (`_tickets_handler`)
- Modify: `jigga/runtime/capabilities.py` (`tickets` action list + `action_inputs`)
- Test: `tests/test_tickets_close.py` (create)

**Interfaces:**
- Consumes: `role_of` (Task 1), `update_task`
- Produces: action `tickets.close` with input `{ticket, comment?}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tickets_close.py
from __future__ import annotations

from pathlib import Path

import pytest

from jigga.core.models import WorkflowStep
from jigga.runtime.capabilities import bundled_capabilities
from jigga.runtime.handlers import _tickets_handler
from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.tasks import create_task, find_task


def _cap():
    return next(c for c in bundled_capabilities() if "tickets.close" in c.actions)


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
                        tools=["tickets.close"], permissions={})
    return RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                          sessions_dir=paths.home / "sessions")


def _close(paths, actor, payload):
    return _tickets_handler(WorkflowStep(id="s", action="tickets.close", input={}),
                            _cap(), payload, {}, _runtime(paths, actor))


def test_the_lead_closes_a_ready_ticket(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "ship", assignee="eng-lead", lane="ready-for-pr",
                    metadata={"team_id": "eng"})

    _close(paths, "eng-lead", {"ticket": t.id})

    fresh = find_task(paths.tasks, t.id)
    assert fresh.lane == "done"
    assert fresh.state == "completed"


def test_only_the_lead_may_close(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "ship", assignee="eng-dev", lane="ready-for-pr",
                    metadata={"team_id": "eng"})
    with pytest.raises(PermissionError):
        _close(paths, "eng-dev", {"ticket": t.id})
    assert find_task(paths.tasks, t.id).state != "completed"


def test_a_ticket_must_reach_ready_for_pr_first(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "ship", assignee="eng-lead", lane="in-progress",
                    metadata={"team_id": "eng"})
    with pytest.raises(ValueError):
        _close(paths, "eng-lead", {"ticket": t.id})
    assert find_task(paths.tasks, t.id).state != "completed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tickets_close.py -v`
Expected: FAIL — `StopIteration` in `_cap()`

- [ ] **Step 3: Write minimal implementation**

Add `"tickets.close"` to the `tickets` capability actions and its `action_inputs`:

```python
            "tickets.close": {
                "ticket": {"type": "string", "required": True,
                           "description": "Id of the ticket to close. Lead only, and only from ready-for-pr."},
                "comment": {"type": "string", "description": "How the work was confirmed done."},
            },
```

In `_tickets_handler`:

```python
    if action == "close":
        from jigga.runtime.lanes import role_of, team_for_task
        from jigga.runtime.tasks import find_task, update_task

        task_id = str(payload.get("ticket") or payload.get("task") or "").strip()
        if not task_id:
            raise ValueError("tickets.close needs a 'ticket' id.")
        task = find_task(tasks_dir, task_id)
        if task is None:
            raise ValueError(f"Ticket not found: {task_id}")
        _team_id, team = team_for_task(teams_dir, task)

        # Closing is what makes a ticket complete, so it is the one action that
        # must not be reachable by accident: the lead owns it, and only from the
        # lane that means QA has passed.
        if role_of(team, actor or "") != "lead":
            append_event(runtime.logs_dir, "ticket.close.refused", status="deny", agent=actor,
                         task_id=task.id, reason="not the team lead")
            raise PermissionError("Only the team lead closes a ticket.")
        if task.lane != "ready-for-pr":
            append_event(runtime.logs_dir, "ticket.close.refused", status="deny", agent=actor,
                         task_id=task.id, reason=f"lane={task.lane!r}, expected 'ready-for-pr'")
            raise ValueError(f"A ticket closes from 'ready-for-pr', not {task.lane!r}.")

        updated = update_task(tasks_dir, task_id, lane="done", state="completed")
        append_event(runtime.logs_dir, "team.ticket.closed", agent=actor, task_id=task.id)
        return {"source": "capability.tickets", "ticket": updated.id, "lane": "done",
                "state": "completed"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tickets_close.py -v`
Expected: 3 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check . && python -m pytest -q
git add jigga/runtime/handlers.py jigga/runtime/capabilities.py tests/test_tickets_close.py
git commit -m "Add tickets.close as the only path to completed

Closing is what makes a ticket complete, so it is the one action that
must not be reachable by accident: the lead owns it, and only from
ready-for-pr. Both refusals are audited."
```

---

### Task 6: Stop spawning a ticket per handoff

**Files:**
- Modify: `jigga/runtime/handoffs.py` (`fire_handoffs`, after the `team is None` guard ~line 92)
- Test: `tests/test_handoffs.py` (modify — read the existing tests first)

**Interfaces:**
- Consumes: `team_lanes` from `lanes.py`
- Produces: no signature change; `fire_handoffs` returns `[]` for lane-managed teams

- [ ] **Step 1: Write the failing test**

Append to `tests/test_handoffs.py` (match its existing fixture style — read the file first and reuse its team/task helpers rather than inventing new ones):

```python
def test_a_lane_managed_team_does_not_spawn_handoff_tickets(tmp_path: Path) -> None:
    """One ticket travels the board. The 2026-08-26 end-to-end produced four
    tickets for one request, three of them handoff spawn."""
    paths = init_runtime(tmp_path)
    write_yaml(paths.home / "teams" / "eng.yaml", {
        "id": "eng", "name": "Eng",
        "agents": [{"id": "eng-dev", "role": "dev"}, {"id": "eng-test", "role": "test"}],
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"}, {"id": "done"}],
        "handoffs": [{"from": "eng-dev", "to": "eng-test", "task": "verify"}],
    })
    before = len(list_tasks(paths.tasks))

    created = fire_handoffs(paths.home, paths.logs, paths.tasks, paths.home / "teams",
                            team_id="eng", from_member="eng-dev")

    assert created == []
    assert len(list_tasks(paths.tasks)) == before
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_handoffs.py -v -k lane_managed`
Expected: FAIL — a task is created, so `created` is non-empty

- [ ] **Step 3: Write minimal implementation**

In `jigga/runtime/handoffs.py`, immediately after the `if team is None: return []` guard:

```python
    # A lane-managed team hands work on by reassigning the ticket it already
    # has (tickets.handoff), so spawning a second task for the same work would
    # fragment the board — one request produced four tickets before this. The
    # mechanism stays for teams with no lanes.
    from jigga.runtime.lanes import team_lanes
    if team_lanes(team):
        append_event(logs_dir, "team.handoff.skipped", team=team_id, from_member=from_member,
                     reason="lane-managed team hands off by reassigning the ticket")
        return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_handoffs.py -v`
Expected: all pass. Existing handoff tests that use a team WITH lanes will now get `[]` — read each failure and decide whether the test's team should declare lanes at all; most will not, and those keep working unchanged.

- [ ] **Step 5: Lint and commit**

```bash
ruff check . && python -m pytest -q
git add jigga/runtime/handoffs.py tests/test_handoffs.py
git commit -m "Stop spawning a ticket per handoff on lane-managed teams

One request produced four tickets, three of them handoff spawn. A team
with lanes hands work on by reassigning the ticket it already has; the
mechanism stays for teams without a board."
```

---

### Task 7: Team config and role instructions

**Files:**
- Modify: `~/.jigga/teams/engineering-team.yaml` (live config — via `jigga` CLI, not by hand)
- Modify: `~/.jigga/recipes/engineering-team.md` (via `jigga agents set --recipe`)
- Test: manual verification below

**Interfaces:**
- Consumes: everything above
- Produces: a live team whose agents know to hand off rather than create tickets

- [ ] **Step 1: Add the transition table to the live team**

The default table already covers `lead→dev`, `dev→test`, `test→dev`, `test→lead` and needs no config. Verify that rather than adding redundant YAML:

```bash
cd ~/JIGGA && source .venv/bin/activate && python -c "
from pathlib import Path
from jigga.core.config import load_teams
from jigga.runtime.lanes import derive_lane, lane_transitions
team = load_teams(Path.home()/'.jigga'/'teams')['engineering-team']
print('transitions:', lane_transitions(team))
for a, b in [('engineering-team-lead','engineering-team-dev'),
             ('engineering-team-dev','engineering-team-test'),
             ('engineering-team-test','engineering-team-lead')]:
    print(f'  {a} -> {b}: {derive_lane(team, a, b)}')
"
```

Expected: `in-progress`, `testing`, `ready-for-pr`. If any is `None`, the team's roles do not match `lead`/`dev`/`test` — add an explicit `lane_transitions.rules` block to the team YAML using the real role names.

- [ ] **Step 2: Update each role's instructions via the recipe**

```bash
cd ~/JIGGA && source .venv/bin/activate
jigga agents set engineering-team-lead role "Triages incoming work into tickets and assigns them. Hand a ticket on with tickets.handoff — never create a second ticket for work that already has one. You alone close a ticket, with tickets.close, and only once it is in ready-for-pr and the PR is merged. Leave a comment saying what you decided and why." --recipe
jigga agents set engineering-team-dev role "Implements the ticket assigned to you. When the work is ready for QA, hand the SAME ticket to engineering-team-test with tickets.handoff and a comment covering what changed and how to verify it. Never create a new ticket for work you were handed." --recipe
jigga agents set engineering-team-test role "Verifies the ticket assigned to you. Hand it to engineering-team-lead with tickets.handoff when it passes, or back to engineering-team-dev when it does not, with a comment saying exactly what you ran and what you saw." --recipe
```

- [ ] **Step 3: Grant the new actions to the team**

```bash
for a in engineering-team-lead engineering-team-dev engineering-team-test; do
  jigga agents get "$a" tools
done
```

For each agent, append `tickets.handoff` (all three) and `tickets.close` (lead only) to its `tools` list via `jigga agents set <id> tools '<json array>' --recipe`, preserving every tool already there — read the current list first and add to it rather than replacing.

- [ ] **Step 4: Verify the recipe carries the change**

```bash
grep -c "tickets.handoff" ~/.jigga/recipes/engineering-team.md
```

Expected: at least 3.

- [ ] **Step 5: Commit the repo-side recipe if one is tracked**

```bash
cd ~/JIGGA && git status --short
# If examples/ ships an engineering-team recipe, mirror the same edits there and commit.
git add -A && git commit -m "Teach the engineering team to hand tickets on rather than spawn them"
```

---

### Task 8: End-to-end — one ticket walks the board

**Files:**
- Test: `tests/test_ticket_board_walk.py` (create)

**Interfaces:**
- Consumes: everything above

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ticket_board_walk.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ticket_board_walk.py -v`
Expected: FAIL until Tasks 1–5 are done; PASS once they are.

- [ ] **Step 3: No implementation** — this task only proves the others compose.

- [ ] **Step 4: Run the whole suite**

Run: `ruff check . && python -m pytest -q`
Expected: all green.

- [ ] **Step 5: Commit and open the PR**

```bash
git add tests/test_ticket_board_walk.py
git commit -m "End-to-end: one ticket walks backlog to done

Proves the pieces compose -- one row for one piece of work, QA rejection
returns it to the author, and only tickets.close completes it."
git push -u origin feat/ticket-lane-lifecycle
gh pr create --base main --title "Ticket lane lifecycle: one ticket travels the board"
```

PR body must state: `completed` now means what it says, so the board's completed count will drop; existing tickets are not migrated; and tickets can now stall in `ready-for-pr` until the lead closes them.

---

## Verification after merge

Deploy is not part of this plan — `~/jigga-stable` must be moved to the merged main SHA and `jigga-supervisor.service` restarted, or none of this is live. Then re-run the live end-to-end: file one ticket for the engineering team and confirm the board shows **one** row moving backlog → in-progress → testing → ready-for-pr → done, rather than four rows each marked completed.

## Stage 2

Comments (`Task.comments`, `tickets.comment`, `jigga task comment`, and the jiggaview ticket dialog) are a separate plan against the same spec, written after this one lands.

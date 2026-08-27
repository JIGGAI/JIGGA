# Lead Decomposes Complex Work — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a team lead break one complex ticket into linked story tickets, with a plan summary on the epic, and have the epic wait until its stories finish.

**Architecture:** A new `tickets.decompose` action creates the stories, links parent and children through `metadata`, rewrites the epic's description into a status page, and parks the epic in a new `waiting` state. When the last child completes — or any child fails — the runtime releases the epic to the lead in the close lane. A dedicated verb rather than a carve-out in the `task.assign` refusal, so intent is never ambiguous.

**Tech Stack:** Python 3.11+, pytest, ruff. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-27-lead-decomposes-complex-work-design.md`

## Global Constraints

- **Work on the remote box.** `control@100.103.210.102`, repo `~/JIGGA`, branch `feat/lead-decomposes-complex-work` (already checked out — do NOT create one). `source ~/JIGGA/.venv/bin/activate` before pytest/ruff. `~/jigga-stable` is production and is NOT touched by this plan.
- **`ruff check .` must pass before every commit.** CI lints before it tests, so a lint error fails all three matrix legs without running a single test. Watch F401 (unused import) and E741 (`l` as a name).
- **Never hardcode a lane id in core.** `DEFAULT_LANE_TRANSITIONS` was removed from `lanes.py` precisely so core stops asserting board shapes. Derive every lane from the team's own rules. The one permitted literal is `DONE_LANE`, which already exists.
- **Check permissions before assuming an agent "won't" do something.** `tickets.*` needs `permissions.tickets: move` as well as the tool grant; `jigga agents tools <id>` shows ✓/✗ per action.
- **Never write a code path that compensates for a denial or a config gap.** Fix the cause.
- **Every new capability action declares `action_inputs`.** An action advertised as an open object gets its arguments guessed and silently dropped.
- **Non-lifecycle teams must be untouched.** Every behaviour here is gated on `is_lifecycle_managed(team)`.

---

### Task 1: The `waiting` state

**Files:**
- Modify: `jigga/core/models.py` (the `TaskState` literal, line 8)
- Test: `tests/test_waiting_state.py` (create)

**Interfaces:**
- Produces: `"waiting"` as a valid `TaskState`; `validate_task_state("waiting")` returns it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_waiting_state.py
"""`waiting` means "this ticket is waiting on its children".

It is deliberately not `blocked`: blocked means "bounced too often, a human must
look", and overloading it would make a healthy epic indistinguishable from a
stuck ticket. It is deliberately not `pending`: the supervisor selects pending
tasks, so a waiting epic would be woken every tick, bounce, and block itself
while its stories were still being built.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.models import validate_task_state
from jigga.runtime.tasks import create_task, set_task_state, tasks_for_agent


def test_waiting_is_a_valid_task_state() -> None:
    assert validate_task_state("waiting") == "waiting"


def test_an_invalid_state_is_still_rejected() -> None:
    with pytest.raises(ValueError):
        validate_task_state("dawdling")


def test_the_supervisor_does_not_pick_up_a_waiting_ticket(tmp_path: Path) -> None:
    """The whole reason for the state: a waiting epic must not be woken."""
    paths = init_runtime(tmp_path)
    task = create_task(paths.tasks, "epic", assignee="eng-lead", lane="in-progress")
    set_task_state(paths.tasks, task.id, "waiting")

    assert [t.id for t in tasks_for_agent(paths.tasks, "eng-lead")] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/JIGGA && source .venv/bin/activate && python -m pytest tests/test_waiting_state.py -v`
Expected: FAIL — `validate_task_state("waiting")` raises, because `waiting` is not in the literal.

- [ ] **Step 3: Write minimal implementation**

In `jigga/core/models.py`, add `"waiting"` to the `TaskState` literal:

```python
TaskState = Literal["pending", "claimed", "running", "blocked", "waiting", "needs_approval", "failed", "completed", "archived"]
```

Read the lines around it first — if a docstring or comment enumerates the states, add `waiting` there too with the one-line meaning: "waiting on child tickets; the supervisor does not wake it".

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_waiting_state.py -v`
Expected: 3 passed.

- [ ] **Step 5: Lint, full suite, commit**

```bash
ruff check . && python -m pytest -q
git add jigga/core/models.py tests/test_waiting_state.py
git commit -m "Add a waiting state for a ticket blocked on its children

Not blocked, which means a human must look, and not pending, which the
supervisor would wake every tick until the ticket bounced itself into
blocked while its stories were still being built."
```

---

### Task 2: Decomposition — validation and the story records

**Files:**
- Create: `jigga/runtime/decompose.py`
- Test: `tests/test_decompose.py` (create)

**Interfaces:**
- Consumes: `create_task`, `update_task`, `find_task` from `jigga.runtime.tasks`; `derive_lane`, `is_lifecycle_managed`, `role_of`, `team_lanes` from `jigga.runtime.lanes`
- Produces:
  - `MAX_STORIES = 20`
  - `class DecomposeError(ValueError)`
  - `decompose(tasks_dir, teams_dir, *, ticket_id, actor, summary, plan, stories) -> dict` returning `{"epic": str, "stories": list[str], "lane": str | None}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_decompose.py
"""A lead breaks one complex ask into linked stories.

Before this, the lead's only options were tickets.handoff (give the whole thing
to one agent) or task.assign (create an unrelated ticket, and now refused while
holding a lane-managed ticket). "Build a new website" went to one dev as a
single ticket.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.decompose import DecomposeError, decompose
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

STORIES = [
    {"title": "Scaffold the app", "description": "Full brief with an acceptance check.",
     "assignee": "eng-dev"},
    {"title": "Build the nav", "description": "Another full brief.", "assignee": "eng-dev"},
]


def _setup(tmp_path: Path, lanes=PIPELINE, transitions=TRANSITIONS, agents=ROSTER):
    paths = init_runtime(tmp_path)
    data = {"id": "eng", "name": "Eng", "agents": agents, "lanes": lanes}
    if transitions is not None:
        data["lane_transitions"] = transitions
    write_yaml(paths.teams / "eng.yaml", data)
    return paths


def _epic(paths, lane="backlog"):
    return create_task(paths.tasks, "New website", description="## Requirements\nA website.",
                       assignee="eng-lead", lane=lane, metadata={"team_id": "eng"})


def _run(paths, epic_id, actor="eng-lead", stories=None, summary="Cut by surface.",
         plan="shared-context/plans/new-website.md"):
    return decompose(paths.tasks, paths.teams, ticket_id=epic_id, actor=actor,
                     summary=summary, plan=plan, stories=stories or STORIES)


def test_each_story_becomes_a_ticket_linked_to_the_epic(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = _epic(paths)

    result = _run(paths, epic.id)

    assert len(result["stories"]) == 2
    for sid, spec in zip(result["stories"], STORIES):
        story = find_task(paths.tasks, sid)
        assert story.title == spec["title"]
        assert story.description == spec["description"]
        assert story.assignee == spec["assignee"]
        assert story.state == "pending"
        assert story.metadata["parent"] == epic.id
        assert story.metadata["team_id"] == "eng"


def test_stories_start_in_the_first_lane_not_handed_off(tmp_path: Path) -> None:
    """Decomposition creates work; moving it is the board's job."""
    paths = _setup(tmp_path)
    epic = _epic(paths)
    result = _run(paths, epic.id)
    assert {find_task(paths.tasks, s).lane for s in result["stories"]} == {"backlog"}


def test_the_epic_waits_in_the_derived_work_lane(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = _epic(paths)

    result = _run(paths, epic.id)

    fresh = find_task(paths.tasks, epic.id)
    assert fresh.state == "waiting"
    assert fresh.lane == "in-progress"          # derived from the lead->dev rule
    assert result["lane"] == "in-progress"
    assert fresh.metadata["children"] == result["stories"]
    assert fresh.metadata["plan"] == "shared-context/plans/new-website.md"


def test_an_underivable_work_lane_leaves_the_epic_where_it_is(tmp_path: Path) -> None:
    """Core must not invent a lane. A team with no lead->dev rule keeps its epic
    in place rather than being handed a column it never declared."""
    paths = _setup(tmp_path, transitions={"rules": [{"from": "dev", "to": "test",
                                                     "lane": "testing"}],
                                          "bounce_lane": "backlog"})
    epic = _epic(paths)
    result = _run(paths, epic.id)
    assert find_task(paths.tasks, epic.id).lane == "backlog"
    assert result["lane"] is None


def test_only_the_lead_may_decompose(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = _epic(paths)
    with pytest.raises(DecomposeError, match="lead"):
        _run(paths, epic.id, actor="eng-dev")
    assert len(list_tasks(paths.tasks)) == 1


def test_every_story_needs_a_brief(tmp_path: Path) -> None:
    """A story without a description is the six-word-ticket bug again."""
    paths = _setup(tmp_path)
    epic = _epic(paths)
    with pytest.raises(DecomposeError, match="description"):
        _run(paths, epic.id, stories=[{"title": "vague", "assignee": "eng-dev"}])
    assert len(list_tasks(paths.tasks)) == 1


def test_a_summary_is_required(tmp_path: Path) -> None:
    """A bare path makes the board unreadable without a second lookup."""
    paths = _setup(tmp_path)
    epic = _epic(paths)
    with pytest.raises(DecomposeError, match="summary"):
        _run(paths, epic.id, summary="   ")


def test_decomposing_twice_is_refused(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = _epic(paths)
    _run(paths, epic.id)
    with pytest.raises(DecomposeError, match="already"):
        _run(paths, epic.id)
    assert len(list_tasks(paths.tasks)) == 3      # epic + 2, not 5


def test_the_story_cap_is_enforced(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = _epic(paths)
    many = [{"title": f"s{i}", "description": "brief", "assignee": "eng-dev"} for i in range(21)]
    with pytest.raises(DecomposeError, match="20"):
        _run(paths, epic.id, stories=many)
    assert len(list_tasks(paths.tasks)) == 1


def test_a_story_assignee_must_be_on_the_team(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    epic = _epic(paths)
    with pytest.raises(DecomposeError, match="stranger"):
        _run(paths, epic.id, stories=[{"title": "s", "description": "b", "assignee": "stranger"}])


def test_a_non_lifecycle_team_cannot_decompose(tmp_path: Path) -> None:
    paths = _setup(tmp_path, transitions=None)
    epic = _epic(paths)
    with pytest.raises(DecomposeError, match="board"):
        _run(paths, epic.id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_decompose.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jigga.runtime.decompose'`

- [ ] **Step 3: Write minimal implementation**

```python
# jigga/runtime/decompose.py
"""Break one complex ticket into linked story tickets.

The lead had no way to split work. `tickets.handoff` gives the whole ticket to
one agent; `task.assign` creates an unrelated ticket and is refused outright
while the lead holds a lane-managed one — which is exactly this situation. So a
complex ask went to a single dev as a single ticket, or nowhere.

This is a separate verb rather than a hole in that refusal, because the refusal
exists to remove a judgment call, and a carve-out would put it back at the
moment the model is already reaching for the wrong tool.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_teams
from jigga.runtime.lanes import derive_lane, is_lifecycle_managed, role_of, team_lanes
from jigga.runtime.tasks import create_task, find_task, update_task

# A confused lead should not be able to flood the board.
MAX_STORIES = 20


class DecomposeError(ValueError):
    """A decomposition that must not happen. The message is shown to the agent."""


def _lead_of(team) -> str | None:
    for member in team.agents or []:
        if isinstance(member, dict) and member.get("role") == "lead" and member.get("id"):
            return str(member["id"])
    return None


def _first_dev(team) -> str | None:
    for member in team.agents or []:
        if isinstance(member, dict) and member.get("role") == "dev" and member.get("id"):
            return str(member["id"])
    return None


def _work_lane(team) -> str | None:
    """Where an epic sits while its stories are built.

    Derived from the team's own lead-to-builder rule, never hardcoded: core
    stopped asserting board shapes when DEFAULT_LANE_TRANSITIONS came out of
    lanes.py, and writing "in-progress" here would put one straight back.
    """
    lead, dev = _lead_of(team), _first_dev(team)
    if not lead or not dev:
        return None
    return derive_lane(team, lead, dev)


def _render_epic(original: str | None, summary: str, plan: str,
                 stories: list[tuple[str, dict]]) -> str:
    """The epic reads as a status page: what the plan is, where the full one
    lives, and what it was cut into. A path alone would make the board
    unreadable without a second lookup, and the plan file is not injected into
    anyone's context."""
    lines = ["## Plan", summary.strip(), "", f"Full plan: {plan}", "", "## Stories"]
    for sid, spec in stories:
        lines.append(f"- {sid}  {spec['title']}  -> {spec['assignee']}")
    if original and original.strip():
        lines += ["", "## Original request", original.strip()]
    return "\n".join(lines)


def decompose(tasks_dir: Path, teams_dir: Path, *, ticket_id: str, actor: str | None,
              summary: str, plan: str, stories: list[dict[str, Any]]) -> dict[str, Any]:
    """Create one story ticket per entry, link them to the epic, and park it."""
    epic = find_task(tasks_dir, ticket_id)
    if epic is None:
        raise DecomposeError(f"Ticket not found: {ticket_id}")
    team_id = (epic.metadata or {}).get("team_id")
    team = load_teams(teams_dir).get(team_id) if team_id else None
    if team is None or not is_lifecycle_managed(team):
        raise DecomposeError(
            f"Ticket {ticket_id} is not on a lifecycle-managed board; there is nothing to "
            "decompose into.")
    if role_of(team, actor or "") != "lead":
        raise DecomposeError("Only the team lead decomposes a ticket.")
    if (epic.metadata or {}).get("children"):
        raise DecomposeError(f"Ticket {ticket_id} has already been decomposed.")
    if not summary or not summary.strip():
        raise DecomposeError("A plan summary is required — the epic has to read on its own.")
    if not plan or not plan.strip():
        raise DecomposeError("A path to the full plan is required.")
    if not stories:
        raise DecomposeError("Decomposing needs at least one story.")
    if len(stories) > MAX_STORIES:
        raise DecomposeError(f"At most {MAX_STORIES} stories; got {len(stories)}.")

    members = {str(m.get("id")) for m in (team.agents or [])
               if isinstance(m, dict) and m.get("id")}
    for spec in stories:
        if not str(spec.get("title") or "").strip():
            raise DecomposeError("Every story needs a title.")
        if not str(spec.get("description") or "").strip():
            raise DecomposeError(
                f"Story {spec.get('title')!r} needs a description: the brief the assignee "
                "works from, with its acceptance check.")
        if str(spec.get("assignee") or "") not in members:
            raise DecomposeError(
                f"Story {spec.get('title')!r} is assigned to {spec.get('assignee')!r}, "
                "a stranger to this team.")

    lanes = team_lanes(team)
    first_lane = lanes[0].id if lanes else None
    created: list[tuple[str, dict]] = []
    for spec in stories:
        story = create_task(
            tasks_dir, str(spec["title"]), description=str(spec["description"]),
            assignee=str(spec["assignee"]), lane=first_lane,
            metadata={"team_id": team_id, "parent": epic.id, "assigned_by": actor})
        created.append((story.id, spec))

    metadata = dict(epic.metadata or {})
    metadata["children"] = [sid for sid, _ in created]
    metadata["plan"] = plan
    lane = _work_lane(team)
    update_task(tasks_dir, epic.id, state="waiting", metadata=metadata,
                description=_render_epic(epic.description, summary, plan, created),
                **({"lane": lane} if lane else {}))
    return {"epic": epic.id, "stories": [sid for sid, _ in created], "lane": lane}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_decompose.py -v`
Expected: 11 passed.

- [ ] **Step 5: Lint, full suite, commit**

```bash
ruff check . && python -m pytest -q
git add jigga/runtime/decompose.py tests/test_decompose.py
git commit -m "Break a complex ticket into linked story tickets

The lead had no way to split work: handoff gives the whole ticket to one
agent, and task.assign is refused while holding a lane-managed one. The
epic is rewritten to carry the plan summary, the path to the full plan
and its story list, so it reads on its own."
```

---

### Task 3: Release the epic when its children finish

**Files:**
- Modify: `jigga/runtime/decompose.py` (append)
- Test: `tests/test_decompose_release.py` (create)

**Interfaces:**
- Consumes: everything from Task 2
- Produces: `release_parent_if_ready(tasks_dir, teams_dir, child_id) -> dict | None` returning `{"epic": str, "reason": str}` when it released one, else `None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_decompose_release.py
"""An epic wakes exactly once: when its stories are finished, or when one dies.

A failed child must release it immediately. Waiting for a story that will never
complete would park the epic forever — the silent stall this whole line of work
exists to remove.
"""
from __future__ import annotations

from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.decompose import decompose, release_parent_if_ready
from jigga.runtime.tasks import create_task, find_task, set_task_state

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
STORIES = [{"title": "one", "description": "brief", "assignee": "eng-dev"},
           {"title": "two", "description": "brief", "assignee": "eng-dev"}]


def _setup(tmp_path: Path):
    paths = init_runtime(tmp_path)
    write_yaml(paths.teams / "eng.yaml", {"id": "eng", "name": "Eng", "agents": ROSTER,
                                          "lanes": PIPELINE, "lane_transitions": TRANSITIONS})
    epic = create_task(paths.tasks, "New website", description="A website.",
                       assignee="eng-lead", lane="backlog", metadata={"team_id": "eng"})
    result = decompose(paths.tasks, paths.teams, ticket_id=epic.id, actor="eng-lead",
                       summary="Cut by surface.", plan="plans/x.md", stories=STORIES)
    return paths, epic.id, result["stories"]


def test_the_epic_stays_asleep_until_the_last_child_is_done(tmp_path: Path) -> None:
    paths, epic_id, kids = _setup(tmp_path)

    set_task_state(paths.tasks, kids[0], "completed")
    assert release_parent_if_ready(paths.tasks, paths.teams, kids[0]) is None
    assert find_task(paths.tasks, epic_id).state == "waiting"


def test_the_last_child_releases_it_to_the_lead_in_the_close_lane(tmp_path: Path) -> None:
    """The close lane specifically: tickets.close refuses anything outside it,
    so an epic released into in-progress could never be closed at all."""
    paths, epic_id, kids = _setup(tmp_path)
    for kid in kids:
        set_task_state(paths.tasks, kid, "completed")

    released = release_parent_if_ready(paths.tasks, paths.teams, kids[-1])

    assert released == {"epic": epic_id, "reason": "children complete"}
    epic = find_task(paths.tasks, epic_id)
    assert epic.state == "pending"
    assert epic.lane == "ready-for-pr"
    assert epic.assignee == "eng-lead"


def test_a_failed_child_releases_the_epic_at_once(tmp_path: Path) -> None:
    paths, epic_id, kids = _setup(tmp_path)

    set_task_state(paths.tasks, kids[0], "failed")
    released = release_parent_if_ready(paths.tasks, paths.teams, kids[0])

    assert released is not None
    assert kids[0] in released["reason"]
    epic = find_task(paths.tasks, epic_id)
    assert epic.state == "pending"
    assert epic.assignee == "eng-lead"


def test_a_blocked_child_releases_the_epic_at_once(tmp_path: Path) -> None:
    paths, epic_id, kids = _setup(tmp_path)
    set_task_state(paths.tasks, kids[1], "blocked")
    assert release_parent_if_ready(paths.tasks, paths.teams, kids[1]) is not None
    assert find_task(paths.tasks, epic_id).state == "pending"


def test_a_task_with_no_parent_releases_nothing(tmp_path: Path) -> None:
    paths, _epic_id, _kids = _setup(tmp_path)
    orphan = create_task(paths.tasks, "orphan", assignee="eng-dev", lane="backlog",
                         metadata={"team_id": "eng"})
    assert release_parent_if_ready(paths.tasks, paths.teams, orphan.id) is None


def test_releasing_twice_is_harmless(tmp_path: Path) -> None:
    """The runtime calls this on every child completion; it must be idempotent."""
    paths, epic_id, kids = _setup(tmp_path)
    for kid in kids:
        set_task_state(paths.tasks, kid, "completed")
    assert release_parent_if_ready(paths.tasks, paths.teams, kids[-1]) is not None
    assert release_parent_if_ready(paths.tasks, paths.teams, kids[-1]) is None
    assert find_task(paths.tasks, epic_id).state == "pending"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_decompose_release.py -v`
Expected: FAIL — `ImportError: cannot import name 'release_parent_if_ready'`

- [ ] **Step 3: Write minimal implementation**

Append to `jigga/runtime/decompose.py`:

```python
# States a child can be in that mean it will never complete on its own.
_DEAD_CHILD_STATES = {"failed", "blocked"}


def release_parent_if_ready(tasks_dir: Path, teams_dir: Path,
                            child_id: str) -> dict[str, Any] | None:
    """Wake a waiting epic when its children are finished, or one of them died.

    Called whenever a task reaches a terminal state. Returns None when there is
    nothing to do, which is the common case — most tasks have no parent.

    A failed or blocked child releases the epic immediately rather than leaving
    it asleep: one dead story would otherwise park the ask forever, and a
    silently stalled ticket is the failure this whole design removes.
    """
    child = find_task(tasks_dir, child_id)
    if child is None:
        return None
    parent_id = (child.metadata or {}).get("parent")
    if not parent_id:
        return None
    epic = find_task(tasks_dir, parent_id)
    if epic is None or epic.state != "waiting":
        return None      # already released; this runs on every child completion

    children = [find_task(tasks_dir, cid) for cid in (epic.metadata or {}).get("children") or []]
    dead = [c for c in children if c is not None and c.state in _DEAD_CHILD_STATES]
    if dead:
        reason = f"{dead[0].id} ended {dead[0].state}"
    elif all(c is not None and c.state == "completed" for c in children):
        reason = "children complete"
    else:
        return None

    team_id = (epic.metadata or {}).get("team_id")
    team = load_teams(teams_dir).get(team_id) if team_id else None
    lane = (close_lane(team) or DEFAULT_CLOSE_LANE) if team is not None else None
    update_task(tasks_dir, epic.id, state="pending", assignee=_lead_of(team) if team else epic.assignee,
                **({"lane": lane} if lane else {}))
    return {"epic": epic.id, "reason": reason}
```

Extend the import at the top of the file — do NOT add a second import line for the same module:

```python
from jigga.runtime.lanes import (
    DEFAULT_CLOSE_LANE,
    close_lane,
    derive_lane,
    is_lifecycle_managed,
    role_of,
    team_lanes,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_decompose_release.py -v`
Expected: 6 passed.

- [ ] **Step 5: Lint, full suite, commit**

```bash
ruff check . && python -m pytest -q
git add jigga/runtime/decompose.py tests/test_decompose_release.py
git commit -m "Wake a waiting epic when its children finish, or one dies

A failed or blocked child releases the epic immediately. Waiting for a
story that will never complete would park the ask forever. It is released
into the close lane because tickets.close refuses anything outside it."
```

---

### Task 4: Wire the release into the run loop

**Files:**
- Modify: `jigga/runtime/agent.py` (`_apply_ticket_outcome`)
- Test: `tests/test_decompose_release.py` (append)

**Interfaces:**
- Consumes: `release_parent_if_ready` from Task 3

> **Deliberate difference from the spec.** The spec's file table puts the release
> in `ticket_outcome.py`. It goes in `agent.py` instead, because
> `resolve_ticket_outcome` is a pure decision function — it decides, the caller
> writes — and reading and updating a second task from inside it would break
> that. `_apply_ticket_outcome` is already the place where the decision is
> turned into writes, so the release belongs beside them.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_decompose_release.py`:

```python
def test_a_child_completing_through_a_real_run_releases_the_epic(tmp_path: Path) -> None:
    """The wiring, not just the function: a story closing during an agent run
    must wake its epic without anyone calling the helper by hand."""
    from unittest.mock import patch

    from jigga.runtime.agent import run_agent
    from jigga.runtime.model_router import ModelCallResult

    paths, epic_id, kids = _setup(tmp_path)
    for aid in ("eng-lead", "eng-dev"):
        write_yaml(paths.agents / f"{aid}.yaml", {
            "id": aid, "name": aid, "role": "r", "memory_scope": "task_only",
            "tools": [], "permissions": {}, "permission_mode": "autonomous"})
    # First story already done; the second finishes in this run.
    set_task_state(paths.tasks, kids[0], "completed")
    from jigga.runtime.tasks import update_task
    update_task(paths.tasks, kids[1], lane="done")

    result = ModelCallResult(status="ok", provider="dry_run", model="m",
                             content="done", dry_run=True, tool_calls=[])
    with patch("jigga.runtime.agent.call_model", lambda *a, **k: result):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    epic = find_task(paths.tasks, epic_id)
    assert epic.state == "pending", "the epic should have been woken by the run"
    assert epic.lane == "ready-for-pr"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_decompose_release.py -v -k real_run`
Expected: FAIL — the epic is still `waiting`; nothing calls the helper.

- [ ] **Step 3: Write minimal implementation**

In `jigga/runtime/agent.py`, inside `_apply_ticket_outcome`, immediately before its final `return update_task(...)`, add:

```python
    updated = update_task(tasks_dir, task.id, state=outcome["state"], lane=outcome["lane"],
                          assignee=outcome["assignee"], metadata=metadata)
    # A finished story may be the last one its epic was waiting for.
    if outcome["state"] == "completed":
        from jigga.runtime.decompose import release_parent_if_ready

        released = release_parent_if_ready(tasks_dir, home / "teams", task.id)
        if released:
            append_event(logs_dir, "ticket.children_complete", agent=agent_id,
                         task_id=released["epic"], child=task.id, reason=released["reason"])
    return updated
```

Read the existing tail of that function first: it currently ends with a single `return update_task(...)`. Replace that one statement with the block above — do not leave both.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_decompose_release.py -v`
Expected: 7 passed.

- [ ] **Step 5: Lint, full suite, commit**

```bash
ruff check . && python -m pytest -q
git add jigga/runtime/agent.py tests/test_decompose_release.py
git commit -m "Wake a waiting epic from the run that finishes its last story

Audited as ticket.children_complete, so the wake reads as something the
runtime did rather than something an agent claimed."
```

---

### Task 5: The `tickets.decompose` action

**Files:**
- Modify: `jigga/runtime/handlers.py` (`_tickets_handler`)
- Modify: `jigga/runtime/capabilities.py` (the `tickets` capability)
- Test: `tests/test_tickets_decompose_action.py` (create)

**Interfaces:**
- Consumes: `decompose`, `DecomposeError` from Task 2
- Produces: action `tickets.decompose` with inputs `{ticket, summary, plan, stories}`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tickets_decompose_action.py
"""The action the lead actually calls.

The dispatch trap: _tickets_handler switches on payload["action"] (default
"move"), NOT step.action. A branch written as `if action == "decompose"` is
unreachable, because callers pass WorkflowStep(action="tickets.decompose") with
no "action" key in the payload. handoff and close both hit this; follow their
pattern.
"""
from __future__ import annotations

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


def test_it_requires_its_arguments(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    with pytest.raises(ValueError):
        _act(paths, "eng-lead", {"ticket": "task_x"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tickets_decompose_action.py -v`
Expected: FAIL — `StopIteration` in `_cap()`; no bundled capability declares `tickets.decompose`.

- [ ] **Step 3: Write minimal implementation**

In `jigga/runtime/capabilities.py`, add `"tickets.decompose"` to the `tickets` capability's `actions` list, and add this entry to its existing `action_inputs` dict — leave the `tickets.handoff` and `tickets.close` entries exactly as they are:

```python
            "tickets.decompose": {
                "ticket": {"type": "string", "required": True,
                           "description": "Id of the complex ticket to break up. It waits "
                                          "until every story you create is finished."},
                "summary": {"type": "string", "required": True,
                            "description": "A few lines: the approach, and why the work is cut "
                                           "this way. This is written onto the ticket, so it has "
                                           "to read on its own."},
                "plan": {"type": "string", "required": True,
                         "description": "Path to the full plan you wrote, e.g. "
                                        "shared-context/plans/<name>.md"},
                "stories": {"type": "array", "required": True,
                            "description": "One entry per story: {title, description, assignee}. "
                                           "The description is the assignee's whole brief "
                                           "including its acceptance check — they will not read "
                                           "the plan file."},
            },
```

Also extend the capability's `when_to_use` so it distinguishes the three verbs — read the current text and add: `"Use tickets.decompose when a ticket is too big for one agent: it creates a story ticket per piece and the original waits for them. Use tickets.handoff when one ticket moves to the next agent as-is."`

In `jigga/runtime/handlers.py`, inside `_tickets_handler`, add before the `move` branch:

```python
    if action == "decompose" or (_step is not None and _step.action == "tickets.decompose"):
        from jigga.runtime.decompose import DecomposeError, decompose

        ticket_id = str(payload.get("ticket") or payload.get("task") or "").strip()
        stories = payload.get("stories")
        if not ticket_id or not isinstance(stories, list):
            raise ValueError("tickets.decompose needs a 'ticket' id and a 'stories' list.")
        try:
            result = decompose(tasks_dir, teams_dir, ticket_id=ticket_id, actor=actor,
                               summary=str(payload.get("summary") or ""),
                               plan=str(payload.get("plan") or ""), stories=stories)
        except DecomposeError as exc:
            append_event(runtime.logs_dir, "ticket.decompose.refused", status="deny",
                         agent=actor, task_id=ticket_id, reason=str(exc))
            raise
        append_event(runtime.logs_dir, "ticket.decomposed", agent=actor, task_id=ticket_id,
                     stories=result["stories"], lane=result["lane"])
        return {"source": "capability.tickets", **result}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_tickets_decompose_action.py -v`
Expected: 4 passed.

- [ ] **Step 5: Lint, full suite, commit**

```bash
ruff check . && python -m pytest -q
git add jigga/runtime/handlers.py jigga/runtime/capabilities.py tests/test_tickets_decompose_action.py
git commit -m "Expose tickets.decompose to the lead

Declares its arguments, so the model is told the field names rather than
guessing them, and says when to decompose versus hand off. Refusals are
audited before they are raised."
```

---

### Task 6: Teach the board, grant the action, and walk an epic end to end

**Files:**
- Modify: `jigga/runtime/lanes.py` (`render_lanes`)
- Test: `tests/test_epic_board_walk.py` (create)
- Config: both teams, via the CLI

**Interfaces:**
- Consumes: everything above

- [ ] **Step 1: Write the failing test**

```python
# tests/test_epic_board_walk.py
"""One epic, three stories, one lap.

Proves the pieces compose: the epic sleeps while its stories are built, wakes
exactly once when the last finishes, and closes through the ordinary path.
"""
from __future__ import annotations

from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import TeamConfig
from jigga.runtime.decompose import decompose, release_parent_if_ready
from jigga.runtime.lanes import render_lanes
from jigga.runtime.tasks import create_task, find_task, list_tasks, set_task_state

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


def test_the_board_says_when_to_decompose() -> None:
    team = TeamConfig.from_dict({"id": "eng", "name": "Eng", "agents": ROSTER,
                                 "lanes": PIPELINE, "lane_transitions": TRANSITIONS})
    text = render_lanes(team)
    assert "tickets.decompose" in text
    assert "tickets.handoff" in text, "both verbs, so the choice is visible"


def test_an_epic_sleeps_through_its_stories_and_wakes_once(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    write_yaml(paths.teams / "eng.yaml", {"id": "eng", "name": "Eng", "agents": ROSTER,
                                          "lanes": PIPELINE, "lane_transitions": TRANSITIONS})
    epic = create_task(paths.tasks, "New website", description="A website.",
                       assignee="eng-lead", lane="backlog", metadata={"team_id": "eng"})

    result = decompose(paths.tasks, paths.teams, ticket_id=epic.id, actor="eng-lead",
                       summary="Cut by surface.", plan="shared-context/plans/site.md",
                       stories=[{"title": f"story {i}", "description": "brief",
                                 "assignee": "eng-dev"} for i in range(3)])

    # The board shows the ask plus its pieces, and the ask is readable.
    assert len(list_tasks(paths.tasks)) == 4
    text = find_task(paths.tasks, epic.id).description
    assert "Cut by surface." in text
    assert "shared-context/plans/site.md" in text
    assert "Original request" in text

    # It sleeps while the first two are built.
    for kid in result["stories"][:2]:
        set_task_state(paths.tasks, kid, "completed")
        assert release_parent_if_ready(paths.tasks, paths.teams, kid) is None
        assert find_task(paths.tasks, epic.id).state == "waiting"

    # ...and wakes on the last, in the lane the lead can close from.
    set_task_state(paths.tasks, result["stories"][2], "completed")
    assert release_parent_if_ready(paths.tasks, paths.teams, result["stories"][2]) is not None
    epic_now = find_task(paths.tasks, epic.id)
    assert (epic_now.state, epic_now.lane, epic_now.assignee) == ("pending", "ready-for-pr", "eng-lead")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_epic_board_walk.py -v`
Expected: FAIL on the first test — `render_lanes` does not mention `tickets.decompose`.

- [ ] **Step 3: Write minimal implementation**

In `jigga/runtime/lanes.py`, in `render_lanes`, inside the lifecycle-managed block, after the existing `Hand it on with tickets.handoff...` lines, add:

```python
    lines.append("When a ticket is too big for one agent, the lead breaks it up with")
    lines.append("`tickets.decompose(ticket, summary, plan, stories)`: it creates a ticket per")
    lines.append("story and the original waits until they are all finished.")
```

Read the surrounding lines first and match their wording and width.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_epic_board_walk.py -v && python -m pytest -q`
Expected: 2 passed, then the whole suite green.

- [ ] **Step 5: Grant the action to both leads (live config)**

Only the leads decompose. Read each list first and extend it — never replace, and re-check the permission afterwards, because `--recipe` regenerates from the recipe and has silently dropped a permission before.

```bash
for a in engineering-team-lead seven-development-team-lead; do
  jigga agents get "$a" tools          # read, then append tickets.decompose to THIS list
done
# jigga agents set <id> tools '<the existing list + tickets.decompose>' --recipe
for a in engineering-team-lead seven-development-team-lead; do
  jigga agents tools "$a" | grep tickets      # expect ✓ for handoff, close and decompose
done
jigga doctor | grep -i grant                  # expect no "can't work" line
```

- [ ] **Step 6: Lint, full suite, commit**

```bash
ruff check . && python -m pytest -q
git add jigga/runtime/lanes.py tests/test_epic_board_walk.py
git commit -m "Teach the board when to decompose, and walk an epic end to end

The lead now has three verbs; the board states when each applies, which
is where it already teaches its own rules."
```

---

## Verification after merge

Deploy is not part of this plan: `~/jigga-stable` must be moved to the merged SHA and `jigga-supervisor.service` restarted, or none of this is live.

Then give the lead a genuinely complex ask — one that cannot sensibly be one ticket — and confirm from the audit log, not from the board's appearance: `ticket.decomposed` fires with its story ids, the epic reads with summary, plan path and story list, the supervisor never wakes the lead for the epic while it waits, and `ticket.children_complete` fires exactly once.

If the lead hands the whole thing to one dev instead, check `agent.tool_call.denied` and `jigga agents tools` **before** concluding anything about its judgment — a denied permission and a bad choice look identical from outside, and that mistake has already cost this project a full day.

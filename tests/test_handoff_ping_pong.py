"""Two agents handing the same ticket back and forth is a loop, not progress."""

from __future__ import annotations

import json
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.capabilities import bundled_capabilities
from jigga.runtime.handlers import _tickets_handler
from jigga.runtime.handoff_loop import MAX_PAIR_HANDOFFS, evaluate_handoff_loop, pair_key
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.tasks import create_task, find_task

PIPELINE_TRANSITIONS = {
    "rules": [
        {"from": "lead", "to": "dev", "lane": "in-progress"},
        {"from": "dev", "to": "test", "lane": "testing"},
        {"from": "test", "to": "dev", "lane": "in-progress"},
        {"from": "test", "to": "lead", "lane": "ready-for-pr"},
        {"from": "dev", "to": "lead", "lane": "ready-for-pr"},
    ],
    "bounce_lane": "backlog",
}


def _cap():
    return next(c for c in bundled_capabilities() if "tickets.handoff" in c.actions)


def _setup(tmp_path: Path, *, lead: bool = True):
    paths = init_runtime(tmp_path)
    agents = [{"id": "eng-dev", "role": "dev"}, {"id": "eng-test", "role": "test"}]
    if lead:
        agents.insert(0, {"id": "eng-lead", "role": "lead"})
    write_yaml(paths.home / "teams" / "eng.yaml", {
        "id": "eng", "name": "Eng", "agents": agents,
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "testing"},
                  {"id": "ready-for-pr"}, {"id": "done"}],
        "lane_transitions": PIPELINE_TRANSITIONS,
    })
    return paths


def _handoff(paths, actor, payload):
    agent = AgentConfig(id=actor, name=actor, role="r", memory_scope="task_only",
                        tools=["tickets.handoff"], permissions={})
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    return _tickets_handler(WorkflowStep(id="s", action="tickets.handoff", input={}),
                            _cap(), payload, {}, runtime)


def _events(paths, kind: str) -> list[dict]:
    path = paths.logs / "events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("type") == kind]


def _ping_pong(paths, ticket_id: str, legs: int) -> list[dict]:
    """dev→test→dev→test… for `legs` handoffs, returning each result."""
    out = []
    for i in range(legs):
        actor, other = ("eng-dev", "eng-test") if i % 2 == 0 else ("eng-test", "eng-dev")
        out.append(_handoff(paths, actor, {"ticket": ticket_id, "assignee": other}))
    return out


# --- the decision itself -------------------------------------------------


def test_a_pair_under_the_limit_is_left_alone() -> None:
    counts = {pair_key("dev", "test"): MAX_PAIR_HANDOFFS - 2}
    verdict = evaluate_handoff_loop(counts, [], "dev", "test", lead="lead")
    assert not verdict.intervened


def test_the_pair_is_counted_in_both_directions() -> None:
    # dev→test and test→dev are one loop seen from two sides. Counting them
    # apart would give the loop twice the budget it is supposed to have.
    assert pair_key("dev", "test") == pair_key("test", "dev")


def test_at_the_limit_the_lead_is_asked_to_decide() -> None:
    counts = {pair_key("dev", "test"): MAX_PAIR_HANDOFFS - 1}
    verdict = evaluate_handoff_loop(counts, [], "dev", "test", lead="lead")
    assert verdict.redirect_to == "lead"
    assert not verdict.block
    assert "back and forth" in (verdict.reason or "")


def test_a_pair_the_lead_already_ruled_on_is_blocked() -> None:
    key = pair_key("dev", "test")
    verdict = evaluate_handoff_loop({key: MAX_PAIR_HANDOFFS + 3}, [key], "dev", "test", lead="lead")
    assert verdict.block
    assert verdict.redirect_to is None


def test_a_lead_inside_the_loop_cannot_break_it() -> None:
    # Redirecting a lead-and-dev loop to the lead is the same two agents and
    # the same lap. There is no third party, so the ticket stops instead.
    counts = {pair_key("lead", "dev"): MAX_PAIR_HANDOFFS - 1}
    verdict = evaluate_handoff_loop(counts, [], "lead", "dev", lead="lead")
    assert verdict.block


def test_a_teamless_handoff_to_yourself_is_not_a_loop() -> None:
    assert not evaluate_handoff_loop({}, [], "dev", "dev", lead="lead").intervened


# --- through the real handler --------------------------------------------


def test_a_normal_pipeline_walk_never_trips_the_guard(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "build", assignee="eng-dev", lane="in-progress",
                    metadata={"team_id": "eng"})

    _handoff(paths, "eng-dev", {"ticket": t.id, "assignee": "eng-test"})
    _handoff(paths, "eng-test", {"ticket": t.id, "assignee": "eng-lead"})

    fresh = find_task(paths.tasks, t.id)
    assert fresh.assignee == "eng-lead"
    assert fresh.lane == "ready-for-pr"
    assert fresh.state == "pending"
    assert _events(paths, "ticket.handoff.loop") == []


def test_the_sixth_lap_between_the_same_two_goes_to_the_lead(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "build", assignee="eng-dev", lane="in-progress",
                    metadata={"team_id": "eng"})

    results = _ping_pong(paths, t.id, MAX_PAIR_HANDOFFS)

    # Every earlier leg landed where it was addressed.
    assert [r["assignee"] for r in results[:-1]] == [
        "eng-test" if i % 2 == 0 else "eng-dev" for i in range(MAX_PAIR_HANDOFFS - 1)]
    # The last one did not.
    assert results[-1]["assignee"] == "eng-lead"
    assert find_task(paths.tasks, t.id).assignee == "eng-lead"

    loop = _events(paths, "ticket.handoff.loop")
    assert len(loop) == 1 and loop[0]["status"] == "ask"
    assert loop[0]["details"]["redirected_to"] == "eng-lead"


def test_the_reason_reaches_the_lead_as_a_comment(tmp_path: Path) -> None:
    # The lead is being handed a ticket it did not ask for. Why has to travel
    # with it, or the lead just hands it straight back and the loop resumes.
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "build", assignee="eng-dev", lane="in-progress",
                    metadata={"team_id": "eng"})

    _ping_pong(paths, t.id, MAX_PAIR_HANDOFFS - 1)
    last = _handoff(paths, "eng-test", {"ticket": t.id, "assignee": "eng-dev",
                                        "comment": "still failing"})

    assert "still failing" in last["comment"]
    assert "back and forth" in last["comment"]


def test_looping_again_after_the_lead_ruled_blocks_the_ticket(tmp_path: Path) -> None:
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "build", assignee="eng-dev", lane="in-progress",
                    metadata={"team_id": "eng"})

    _ping_pong(paths, t.id, MAX_PAIR_HANDOFFS)          # → escalates to the lead
    _handoff(paths, "eng-lead", {"ticket": t.id, "assignee": "eng-dev"})  # lead rules
    result = _handoff(paths, "eng-dev", {"ticket": t.id, "assignee": "eng-test"})

    assert result["blocked"] is True
    fresh = find_task(paths.tasks, t.id)
    assert fresh.state == "blocked"
    # It stays with whoever held it — a blocked ticket does not quietly change hands.
    assert fresh.assignee == "eng-dev"

    loop = _events(paths, "ticket.handoff.loop")
    assert [e["status"] for e in loop] == ["ask", "error"]


def test_a_team_with_no_lead_blocks_rather_than_looping(tmp_path: Path) -> None:
    paths = _setup(tmp_path, lead=False)
    t = create_task(paths.tasks, "build", assignee="eng-dev", lane="in-progress",
                    metadata={"team_id": "eng"})

    results = _ping_pong(paths, t.id, MAX_PAIR_HANDOFFS)

    assert results[-1].get("blocked") is True
    assert find_task(paths.tasks, t.id).state == "blocked"


def test_the_count_survives_a_third_agent_joining_in(tmp_path: Path) -> None:
    # A detour through the lead does not launder the pair's history: dev and
    # test resume exactly where they left off.
    paths = _setup(tmp_path)
    t = create_task(paths.tasks, "build", assignee="eng-dev", lane="in-progress",
                    metadata={"team_id": "eng"})

    _ping_pong(paths, t.id, MAX_PAIR_HANDOFFS - 1)
    _handoff(paths, "eng-dev", {"ticket": t.id, "assignee": "eng-lead"})
    _handoff(paths, "eng-lead", {"ticket": t.id, "assignee": "eng-dev"})
    result = _handoff(paths, "eng-dev", {"ticket": t.id, "assignee": "eng-test"})

    assert result["assignee"] == "eng-lead"
    # The redirected attempt is still counted as a dev/test lap: it is what
    # tipped the loop, and pretending it did not happen would let the pair
    # re-earn the same escalation on its next try.
    counts = find_task(paths.tasks, t.id).metadata["handoff_pairs"]
    assert counts[pair_key("eng-dev", "eng-test")] == MAX_PAIR_HANDOFFS

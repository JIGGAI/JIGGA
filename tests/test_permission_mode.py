from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.config import default_permission_mode, load_agents, load_workflows
from jigga.core.models import AgentConfig, validate_permission_mode
from jigga.runtime.policy import (
    APPROVAL_MODES,
    NON_EXECUTING_MODES,
    evaluate_workflow_step,
    resolve_permission_mode,
)
from jigga.runtime.workflow import plan_workflow, run_workflow


def test_validate_permission_mode_accepts_documented_modes() -> None:
    for mode in ("plan_only", "ask", "accept_edits", "autonomous", "locked_down"):
        assert validate_permission_mode(mode) == mode


def test_validate_permission_mode_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        validate_permission_mode("yolo")


def test_agent_config_validates_permission_mode_at_load() -> None:
    with pytest.raises(ValueError):
        AgentConfig.from_dict(
            {"id": "a", "name": "A", "role": "r", "permission_mode": "yolo"}
        )
    agent = AgentConfig.from_dict(
        {"id": "a", "name": "A", "role": "r", "permission_mode": "plan_only"}
    )
    assert agent.permission_mode == "plan_only"


def test_default_permission_mode_reads_runtime_config(tmp_path: Path) -> None:
    init_runtime(tmp_path, examples=True)
    assert default_permission_mode(tmp_path) == "ask"


def test_resolve_permission_mode_falls_back_to_default() -> None:
    bare = AgentConfig(id="bare", name="Bare", role="r")
    assert resolve_permission_mode(bare, "ask") == "ask"
    locked = AgentConfig(id="locked", name="Locked", role="r", permission_mode="locked_down")
    assert resolve_permission_mode(locked, "autonomous") == "locked_down"


def test_workflow_plan_blocks_when_agent_is_plan_only(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agents = load_agents(paths.agents)
    agents["daily_briefing_agent"].permission_mode = "plan_only"
    workflow = load_workflows(paths.workflows)["morning_day_summary"]
    plan = plan_workflow(workflow, agents, default_mode="autonomous")
    assert plan["can_run"] is False
    assert all(
        step["policy"]["permission"] == "permission_mode.plan_only"
        for step in plan["steps"]
        if step["agent"] == "daily_briefing_agent"
    )


def test_workflow_run_returns_needs_approval_when_locked_down(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    # Force the example agent to locked_down via on-the-fly YAML rewrite
    yaml_path = paths.agents / "daily_briefing_agent.yaml"
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8") + "\npermission_mode: locked_down\n",
        encoding="utf-8",
    )
    result = run_workflow(
        paths.home, paths.logs, paths.workflows, paths.agents, paths.memory, "morning_day_summary"
    )
    # `deny` decisions surface as "blocked", which yields a top-level blocked status
    assert result["status"] == "blocked"


def test_mode_constants_are_documented() -> None:
    assert NON_EXECUTING_MODES == frozenset({"plan_only", "locked_down"})
    assert APPROVAL_MODES == frozenset({"ask"})


def test_run_agent_holds_tasks_when_locked_down(tmp_path: Path) -> None:
    from jigga.runtime.agent import run_agent
    from jigga.runtime.tasks import create_task, list_tasks

    paths = init_runtime(tmp_path, examples=True)
    yaml_path = paths.agents / "daily_briefing_agent.yaml"
    yaml_path.write_text(
        yaml_path.read_text(encoding="utf-8") + "\npermission_mode: locked_down\n",
        encoding="utf-8",
    )
    task = create_task(paths.tasks, "Held task", assignee="daily_briefing_agent")
    result = run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "daily_briefing_agent")
    assert result["status"] == "policy_denied"
    assert result["permission_mode"] == "locked_down"
    assert any(held["id"] == task.id for held in result["held_tasks"])
    # Task should be in needs_approval state, not completed
    assert next(t for t in list_tasks(paths.tasks) if t.id == task.id).state == "needs_approval"


def test_run_agent_emits_policy_evaluated_audit(tmp_path: Path) -> None:
    import json
    from jigga.runtime.agent import run_agent
    from jigga.runtime.tasks import create_task

    paths = init_runtime(tmp_path, examples=True)
    create_task(paths.tasks, "Audit me", assignee="daily_briefing_agent")
    run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "daily_briefing_agent")
    events = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    policy_events = [event for event in events if event["type"] == "policy.evaluated"]
    assert policy_events
    assert policy_events[-1]["details"]["permission_mode"] == "ask"
    assert policy_events[-1]["details"]["runtime_default"] == "ask"


def test_existing_workflow_still_runnable_under_default_ask_mode(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    workflow = load_workflows(paths.workflows)["morning_day_summary"]
    plan = plan_workflow(workflow, load_agents(paths.agents), default_mode="ask")
    assert plan["can_run"] is True
    # Default mode surfaces in the plan output
    assert plan["default_permission_mode"] == "ask"
    # Per-step `evaluate_workflow_step` returns allow under ask + no agent override
    first = workflow.steps[0]
    decision = evaluate_workflow_step(first, load_agents(paths.agents)[first.agent], default_mode="ask")
    assert decision.status == "allow"

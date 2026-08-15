"""A grant is only half the story.

The action has to resolve to a registered capability, and that capability's own
declared resource needs have to be satisfiable by the agent's permissions. Miss
either and the grant is decoration — and both failures are quiet:

- an **unregistered** action is filtered out before the model is ever offered
  it, so the agent simply never does that thing and nobody learns why
- a **blocked** one is offered and fails at the moment of use

The shipped recipes carried four of the first kind (`filesystem.read`,
`filesystem.write`, `task.create`, `memory.write_summary` — none of which are
capability actions) and one of the second (the `editor` role granted a
content-drafting action with no filesystem permission at all).
"""

from __future__ import annotations

from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.config import load_agents
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.dispatcher import effective_tools, unusable_grants


def _registry(paths) -> CapabilityRegistry:
    return CapabilityRegistry.load(user_capabilities=paths.capabilities,
                                   approvals_dir=paths.policies)


def _status(rows: list[dict], action: str) -> str:
    return next(r["status"] for r in rows if r["action"] == action)


# --- the unit ---------------------------------------------------------------


def test_a_satisfied_grant_is_ready(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    agent = AgentConfig(id="a", name="A", role="r", tools=["memory.search"],
                        permissions={"memory": {"scope": "task_only"}}, memory_scope="task_only")
    rows = effective_tools(agent, _registry(paths))
    assert _status(rows, "memory.search") == "ready"
    assert unusable_grants(agent, _registry(paths)) == []


def test_an_action_naming_no_capability_is_unregistered(tmp_path: Path) -> None:
    """The exact shape the recipes shipped: a plausible name that resolves to
    nothing and is dropped before the model sees it."""
    paths = init_runtime(tmp_path)
    agent = AgentConfig(id="a", name="A", role="r",
                        tools=["filesystem.read", "task.create", "memory.write_summary"])
    rows = effective_tools(agent, _registry(paths))
    assert {r["status"] for r in rows} == {"unregistered"}
    assert all(r["capability"] is None for r in rows)
    assert len(unusable_grants(agent, _registry(paths))) == 3


def test_a_grant_whose_capability_needs_a_permission_is_blocked(tmp_path: Path) -> None:
    """`content-drafting` declares filesystem paths. Granting one of its actions
    without them offers a tool that fails on first use."""
    paths = init_runtime(tmp_path)
    agent = AgentConfig(id="editor", name="E", role="reviews",
                        tools=["review_tone_and_claims"],
                        permissions={"network": {"mode": "ask"}})
    rows = effective_tools(agent, _registry(paths))
    assert _status(rows, "review_tone_and_claims") == "blocked"
    assert "allow list" in next(r["reason"] for r in rows)


def test_granting_the_permission_makes_it_ready(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    agent = AgentConfig(id="editor", name="E", role="reviews",
                        tools=["review_tone_and_claims"],
                        permissions={"filesystem": {"allow": ["~/Projects/content"]},
                                     "network": {"mode": "ask"}})
    assert _status(effective_tools(agent, _registry(paths)), "review_tone_and_claims") == "ready"
    assert unusable_grants(agent, _registry(paths)) == []


def test_needs_approval_is_not_counted_as_unusable(tmp_path: Path) -> None:
    """Parking for a human is a working state, not a broken one."""
    paths = init_runtime(tmp_path)
    # `media` declares `network: {mode: allow}`; an agent set to `ask` parks.
    agent = AgentConfig(id="a", name="A", role="r", tools=["media.generate_image"],
                        permissions={"network": {"mode": "ask"}})
    rows = effective_tools(agent, _registry(paths))
    assert _status(rows, "media.generate_image") == "needs_approval"
    assert unusable_grants(agent, _registry(paths)) == []


def test_an_agent_granted_nothing_has_nothing(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    agent = AgentConfig(id="a", name="A", role="r", tools=[])
    assert effective_tools(agent, _registry(paths)) == []


def test_permissions_tools_allow_is_included(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    agent = AgentConfig(id="a", name="A", role="r", tools=[],
                        permissions={"tools": {"allow": ["memory.search"]},
                                     "memory": {"scope": "task_only"}},
                        memory_scope="task_only")
    assert [r["action"] for r in effective_tools(agent, _registry(paths))] == ["memory.search"]


# --- the CLI verb -----------------------------------------------------------


def test_agents_tools_reports_and_exits_nonzero_on_a_broken_grant(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "broken.yaml", {
        "id": "broken", "name": "B", "role": "r",
        "tools": ["memory.search", "task.create"],
        "permissions": {"memory": {"scope": "task_only"}}, "memory_scope": "task_only"})
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "agents", "tools", "broken"]) == 1
    out = capsys.readouterr().out
    assert "memory.search" in out and "task.create" in out
    assert "will not work as written" in out


def test_agents_tools_exits_zero_when_everything_works(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "fine.yaml", {
        "id": "fine", "name": "F", "role": "r", "tools": ["memory.search"],
        "permissions": {"memory": {"scope": "task_only"}}, "memory_scope": "task_only"})
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "agents", "tools", "fine"]) == 0
    assert "will not work" not in capsys.readouterr().out


def test_agents_tools_says_so_when_nothing_is_granted(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "mute.yaml", {"id": "mute", "name": "M", "role": "r", "tools": []})
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "agents", "tools", "mute"]) == 0
    assert "it can talk, and nothing else" in capsys.readouterr().out


def test_agents_tools_on_a_missing_agent(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "agents", "tools", "nobody"]) == 1
    assert "No such agent" in capsys.readouterr().out


# --- the doctor check -------------------------------------------------------


def test_doctor_warns_about_grants_that_cannot_work(tmp_path: Path) -> None:
    from jigga.runtime import doctor

    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "broken.yaml", {
        "id": "broken", "name": "B", "role": "r", "tools": ["task.create"]})
    check = doctor._check_agent_tools(paths)
    assert check.status == doctor.WARN
    assert "broken:task.create" in check.detail
    assert "jigga agents tools" in (check.hint or "")


def test_doctor_is_green_when_every_grant_is_usable(tmp_path: Path) -> None:
    from jigga.runtime import doctor

    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "fine.yaml", {
        "id": "fine", "name": "F", "role": "r", "tools": ["memory.search"],
        "permissions": {"memory": {"scope": "task_only"}}, "memory_scope": "task_only"})
    assert doctor._check_agent_tools(paths).status == doctor.OK


# --- the regression guard ---------------------------------------------------


def test_no_bundled_agent_has_a_grant_that_cannot_work(tmp_path: Path) -> None:
    """The check that would have caught it. The shipped recipes granted four
    actions that name no capability, and staffed a review role with no
    filesystem permission for the capability it was given."""
    paths = init_runtime(tmp_path, examples=True)
    registry = _registry(paths)
    problems = [
        f"{agent_id}:{row['action']} ({row['status']})"
        for agent_id, agent in sorted(load_agents(paths.agents).items())
        for row in unusable_grants(agent, registry)
    ]
    assert problems == [], f"bundled agents with unusable grants: {problems}"


def test_every_bundled_workflow_step_has_a_staffed_capable_agent(tmp_path: Path) -> None:
    """A role referenced by a workflow step should exist and hold the tool that
    step calls — otherwise the example ships a pipeline that cannot run."""
    from jigga.core.config import load_workflows
    from jigga.runtime.policy import granted_actions

    paths = init_runtime(tmp_path, examples=True)
    agents = load_agents(paths.agents)
    grants = {a: set(granted_actions(c)) for a, c in agents.items()}
    gaps: list[str] = []
    for workflow_id, workflow in load_workflows(paths.workflows).items():
        steps = list(getattr(workflow, "steps", []) or []) + list(getattr(workflow, "nodes", []) or [])
        for step in steps:
            action, agent_id = getattr(step, "action", None), getattr(step, "agent", None)
            if not action or not agent_id:
                continue
            if agent_id not in agents:
                gaps.append(f"{workflow_id}.{step.id}: agent {agent_id!r} not staffed")
            elif action not in grants[agent_id]:
                gaps.append(f"{workflow_id}.{step.id}: {agent_id} lacks {action}")
    assert gaps == [], f"bundled workflow steps with no capable agent: {gaps}"

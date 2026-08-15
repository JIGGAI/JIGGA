"""The tool grant is a security boundary, not a menu.

`agent.tools` used to gate only which function schemas the model was offered
(`agent.py::_resolve_agent_actions`). Every other execution path — workflow v1,
workflow v2, anything naming an action directly — reached the handler without
consulting it. An agent with `tools: []` could write files through a workflow
while its model-facing tool list showed nothing at all.

These tests pin the boundary at each layer it now exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_workflows
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.dispatcher import RuntimeContext, dispatch_action
from jigga.runtime.policy import evaluate_tool_grant, granted_actions
from jigga.runtime.workflow import plan_workflow, run_workflow
from jigga.runtime.workflow_engine import run_workflow_v2


def _registry(paths) -> CapabilityRegistry:
    return CapabilityRegistry.load(user_capabilities=paths.capabilities, approvals_dir=paths.policies)


def _ungranted_agent(paths, agent_id: str = "nobody") -> None:
    """An agent granted nothing, but with wide-open resource permissions and
    autonomous mode — so anything that runs, runs because the grant check
    didn't stop it."""
    write_yaml(paths.agents / f"{agent_id}.yaml", {
        "id": agent_id, "name": "Nobody", "role": "granted nothing",
        "permission_mode": "autonomous", "tools": [],
        "permissions": {"filesystem": {"allow": [f"{paths.home}/**"]},
                        "network": {"mode": "allow"}},
    })


# --- the unit --------------------------------------------------------------


def test_granted_actions_merges_both_grant_sources() -> None:
    agent = AgentConfig(id="a", name="A", role="r", tools=["memory.search", "memory.search"],
                        permissions={"tools": {"allow": ["notifications.send"]}})
    assert granted_actions(agent) == ["memory.search", "notifications.send"]


def test_granted_actions_treats_a_missing_field_as_nothing() -> None:
    """Agent stand-ins may not carry every field; absent must read as denied
    rather than raising past the check."""
    class _Bare:
        id = "bare"

    assert granted_actions(_Bare()) == []
    assert evaluate_tool_grant(_Bare(), "filesystem.write_file").status == "deny"


def test_evaluate_tool_grant_names_the_fix() -> None:
    agent = AgentConfig(id="scribe", name="S", role="r", tools=[])
    decision = evaluate_tool_grant(agent, "filesystem.write_file")
    assert decision.status == "deny"
    assert decision.permission == "tools.grant"
    assert "filesystem.write_file" in decision.reason
    assert "agents/scribe.yaml" in decision.reason        # tells you exactly where to add it


def test_no_agent_grants_nothing() -> None:
    assert evaluate_tool_grant(None, "shell.run").status == "deny"


# --- workflow v1 -----------------------------------------------------------


def test_v1_workflow_cannot_run_an_ungranted_action(tmp_path: Path) -> None:
    """The original hole, verbatim: an agent granted nothing wrote a file."""
    paths = init_runtime(tmp_path, examples=True)
    _ungranted_agent(paths)
    target = paths.home / "written.txt"
    write_yaml(paths.workflows / "wf.yaml", {
        "id": "wf", "name": "wf", "status": "active",
        "steps": [{"id": "write", "agent": "nobody", "action": "filesystem.write_file",
                   "input": {"path": str(target), "content": "x"}}],
    })
    plan = plan_workflow(load_workflows(paths.workflows)["wf"], load_agents(paths.agents),
                         registry=_registry(paths))
    assert plan["can_run"] is False
    assert plan["steps"][0]["policy"]["permission"] == "tools.grant"

    result = run_workflow(paths, "wf")
    assert result["status"] == "blocked"
    assert not target.exists()


def test_v1_workflow_runs_once_the_grant_is_added(tmp_path: Path, grant) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _ungranted_agent(paths)
    target = paths.home / "written.txt"
    write_yaml(paths.workflows / "wf.yaml", {
        "id": "wf", "name": "wf", "status": "active",
        "steps": [{"id": "write", "agent": "nobody", "action": "filesystem.write_file",
                   "input": {"path": str(target), "content": "granted"}}],
    })
    grant(paths, "nobody", "filesystem.write_file")
    assert run_workflow(paths, "wf")["status"] == "completed"
    assert target.read_text() == "granted"


def test_permissions_tools_allow_is_a_valid_grant_source(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.agents / "nobody.yaml", {
        "id": "nobody", "name": "Nobody", "role": "r", "permission_mode": "autonomous",
        "tools": [],
        "permissions": {"filesystem": {"allow": [f"{paths.home}/**"]},
                        "tools": {"allow": ["filesystem.write_file"]}},
    })
    target = paths.home / "via_perms.txt"
    write_yaml(paths.workflows / "wf.yaml", {
        "id": "wf", "name": "wf", "status": "active",
        "steps": [{"id": "write", "agent": "nobody", "action": "filesystem.write_file",
                   "input": {"path": str(target), "content": "ok"}}],
    })
    assert run_workflow(paths, "wf")["status"] == "completed"


# --- workflow v2 -----------------------------------------------------------


def test_v2_dag_node_cannot_run_an_ungranted_action(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _ungranted_agent(paths)
    target = paths.home / "v2.txt"
    write_yaml(paths.workflows / "dag.yaml", {
        "id": "dag", "name": "dag", "status": "active",
        "nodes": [{"id": "write", "type": "tool", "agent": "nobody",
                   "action": "filesystem.write_file",
                   "input": {"path": str(target), "content": "x"}}],
        "edges": [],
    })
    record = run_workflow_v2(paths, load_workflows(paths.workflows)["dag"])
    assert record["status"] == "failed"
    assert "not granted" in record["nodes"]["write"]["error"]
    assert not target.exists()


# --- the dispatch floor ----------------------------------------------------


def test_dispatch_action_refuses_an_ungranted_action(tmp_path: Path) -> None:
    """The last gate. `_step_policy` blocks ungranted steps and the agent loop
    only offers granted schemas — but a caller that forgets both must still not
    hand an agent authority it was never given."""
    paths = init_runtime(tmp_path, examples=True)
    agent = AgentConfig(id="nobody", name="N", role="r", tools=[],
                        permissions={"calendar": "read"})
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    with pytest.raises(PermissionError, match="not granted"):
        dispatch_action(WorkflowStep(id="s", action="calendar.list_events"), {}, {},
                        runtime, _registry(paths), paths.logs, run_id="r1")


def test_dispatch_denial_is_audited(tmp_path: Path) -> None:
    import json

    paths = init_runtime(tmp_path, examples=True)
    agent = AgentConfig(id="nobody", name="N", role="r", tools=[],
                        permissions={"calendar": "read"})
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    with pytest.raises(PermissionError):
        dispatch_action(WorkflowStep(id="s", action="calendar.list_events"), {}, {},
                        runtime, _registry(paths), paths.logs, run_id="r1")
    events = [json.loads(line) for line in
              (paths.logs / "events.jsonl").read_text().splitlines() if line.strip()]
    denied = [e for e in events if e["type"] == "capability.invocation.denied"]
    assert denied and denied[-1]["status"] == "deny"
    assert denied[-1]["details"]["permission"] == "tools.grant"
    assert denied[-1]["details"]["agent"] == "nobody"
    # ...and the handler never ran, so no completion was recorded.
    assert not [e for e in events if e["type"] == "capability.invocation.completed"]


def test_engine_internal_dispatch_is_out_of_scope(tmp_path: Path) -> None:
    """Agent-less dispatch is the engine acting on its own behalf (writeback
    nodes and the like), not an agent exercising authority — same carve-out the
    runtime-only check makes."""
    paths = init_runtime(tmp_path, examples=True)
    runtime = RuntimeContext(agent=None, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    out = dispatch_action(WorkflowStep(id="s", action="calendar.list_events"), {}, {},
                          runtime, _registry(paths), paths.logs, run_id="r1")
    assert out is not None


# --- the model-facing list and the boundary agree --------------------------


def test_offered_tools_and_executable_tools_come_from_one_list(tmp_path: Path) -> None:
    """What the model is offered and what the runtime will execute must derive
    from the same grants, or the offer stops describing the boundary."""
    from jigga.runtime.agent import _resolve_agent_actions

    paths = init_runtime(tmp_path, examples=True)
    agent = AgentConfig(id="a", name="A", role="r",
                        tools=["memory.search", "not.a.capability"],
                        permissions={"tools": {"allow": ["notifications.send"]}})
    registry = _registry(paths)
    offered = _resolve_agent_actions(agent, registry)
    assert offered == ["memory.search", "notifications.send"]   # unresolvable name dropped
    for action in offered:
        assert evaluate_tool_grant(agent, action).status == "allow"
    assert evaluate_tool_grant(agent, "filesystem.write_file").status == "deny"


# --- shell stays doubly guarded --------------------------------------------


def test_shell_needs_both_the_grant_and_a_shell_policy(tmp_path: Path, grant) -> None:
    """Command-line access carries a second, independent floor: even a granted
    agent is refused by `safe_process` unless `permissions.shell` allows it."""
    from jigga.tools.safe_process import ProcessPolicyError

    paths = init_runtime(tmp_path, examples=True)
    _ungranted_agent(paths)
    write_yaml(paths.workflows / "sh.yaml", {
        "id": "sh", "name": "sh", "status": "active",
        "steps": [{"id": "run", "agent": "nobody", "action": "shell.run",
                   "input": {"command": ["true"]}}],
    })
    # Ungranted: blocked by the grant check, never reaching the handler.
    assert run_workflow(paths, "sh")["status"] == "blocked"

    # Granted the tool but with no shell policy: the floor still refuses.
    grant(paths, "nobody", "shell.run")
    with pytest.raises(ProcessPolicyError, match="denied"):
        run_workflow(paths, "sh")

"""Model-backed steps declare the shape of their reply, or nothing may consume it.

An untyped model step returns whatever shape the model felt like producing that
day. On the precursor stack one ran correctly for months on one machine and
corrupted a file on another — the node returned `{"markdown_lines": [...]}`
instead of prose, the save step wrote that JSON object into the calendar file,
and the file lost every `### Week N` header the *next* week's workflow parses.
It surfaced a week later, in a different workflow, as a content mismatch.

That class of bug passes every test you write and fails on a model upgrade. The
only defence is refusing to let an untyped reply be consumed at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_workflows
from jigga.core.io import write_yaml
from jigga.core.models import WorkflowStep
from jigga.runtime import handlers
from jigga.runtime.dispatcher import RuntimeContext, register_outputs
from jigga.runtime.handlers import OutputContractError, _draft_with_model_handler
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.workflow import plan_workflow, run_workflow, untyped_model_outputs_consumed


def _reply(content: str) -> ModelCallResult:
    return ModelCallResult(status="ok", provider="dry_run", model="m",
                           content=content, dry_run=True, error=None)


def _returns(content: str):
    return lambda _h, _l, _r: _reply(content)


def _agent(paths) -> RuntimeContext:
    write_yaml(paths.agents / "writer.yaml", {
        "id": "writer", "name": "Writer", "role": "Writes copy.",
        "memory_scope": "task_only", "model": "profile:default",
        "tools": ["draft_with_model"],
        "permissions": {"network": {"mode": "ask"}, "shell": {"mode": "deny"}},
    })
    agent = load_agents(paths.agents)["writer"]
    return RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                          sessions_dir=paths.home / "sessions")


def _step(**kw) -> WorkflowStep:
    kw.setdefault("id", "s1")
    kw.setdefault("action", "draft_with_model")
    return WorkflowStep(**kw)


# --- the handler ------------------------------------------------------------


def test_an_untyped_step_still_returns_raw_prose(tmp_path: Path) -> None:
    """Unchanged for the ending step of a chain, which is the legitimate case."""
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    with patch.object(handlers, "call_model", _returns("just some prose")):
        assert _draft_with_model_handler(_step(), None, "x", {}, runtime) == "just some prose"


def test_a_single_declared_field_returns_that_field(tmp_path: Path) -> None:
    """Chaining stays identical to the untyped form — the consumer receives the
    prose, not a dict it has to know how to unwrap."""
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    step = _step(output_fields=[{"name": "markdown", "type": "text"}])
    with patch.object(handlers, "call_model", _returns('{"markdown": "# Week 1\\n- item"}')):
        assert _draft_with_model_handler(step, None, "x", {}, runtime) == "# Week 1\n- item"


def test_multiple_fields_return_a_dict(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    step = _step(output_fields=[{"name": "title"}, {"name": "body"}])
    with patch.object(handlers, "call_model", _returns('{"title": "T", "body": "B", "extra": 1}')):
        out = _draft_with_model_handler(step, None, "x", {}, runtime)
    assert out == {"title": "T", "body": "B"}      # undeclared keys dropped


def test_the_contract_is_stated_in_the_prompt(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    seen: dict = {}

    def _capture(_h, _l, request):
        seen["system"] = next(i.content for i in request.items if i.role == "system")
        return _reply('{"markdown": "x"}')

    step = _step(output_fields=[{"name": "markdown", "type": "text",
                                 "description": "the calendar body"}])
    with patch.object(handlers, "call_model", _capture):
        _draft_with_model_handler(step, None, "x", {}, runtime)
    assert "single JSON object" in seen["system"]
    assert "markdown (text) — the calendar body" in seen["system"]


def test_prose_where_json_was_declared_fails_loudly(tmp_path: Path) -> None:
    """The woods failure exactly: a reply the consumer would have swallowed."""
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    step = _step(output_fields=[{"name": "markdown"}])
    with patch.object(handlers, "call_model", _returns("# Week 1\n- item")):
        with pytest.raises(OutputContractError) as exc:
            _draft_with_model_handler(step, None, "x", {}, runtime)
    assert "did not return JSON" in str(exc.value)
    assert "# Week 1" in str(exc.value)            # the reply is the evidence


def test_a_missing_declared_field_fails(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    step = _step(output_fields=[{"name": "markdown"}])
    with patch.object(handlers, "call_model", _returns('{"markdown_lines": ["a", "b"]}')):
        with pytest.raises(OutputContractError, match="missing declared field"):
            _draft_with_model_handler(step, None, "x", {}, runtime)


def test_a_json_array_is_not_an_object(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    step = _step(output_fields=[{"name": "markdown"}])
    with patch.object(handlers, "call_model", _returns('["a", "b"]')):
        with pytest.raises(OutputContractError, match="not an object"):
            _draft_with_model_handler(step, None, "x", {}, runtime)


def test_a_fenced_reply_is_tolerated(tmp_path: Path) -> None:
    """Models fence JSON constantly. Refusing that would be pedantry, not safety."""
    paths = init_runtime(tmp_path)
    runtime = _agent(paths)
    step = _step(output_fields=[{"name": "markdown"}])
    with patch.object(handlers, "call_model", _returns('```json\n{"markdown": "ok"}\n```')):
        assert _draft_with_model_handler(step, None, "x", {}, runtime) == "ok"


# --- addressable fields -----------------------------------------------------


def test_each_field_of_a_multi_field_reply_is_addressable() -> None:
    outputs: dict = {}
    step = _step(id="draft", output="draft.md",
                 output_fields=[{"name": "title"}, {"name": "body"}])
    register_outputs(outputs, step, {"title": "T", "body": "B"})
    assert outputs["draft"] == {"title": "T", "body": "B"}
    assert outputs["draft.title"] == "T"
    assert outputs["draft.md.body"] == "B"


def test_a_single_field_step_registers_only_its_own_names() -> None:
    outputs: dict = {}
    register_outputs(outputs, _step(id="draft", output="draft.md",
                                    output_fields=[{"name": "markdown"}]), "prose")
    assert outputs == {"draft": "prose", "draft.md": "prose"}


# --- plan-time rejection ----------------------------------------------------


def _two_step(paths, *, typed: bool, wf_id: str = "chain") -> None:
    producer = {"id": "draft", "agent": "writer", "action": "draft_with_model",
                "input": {"prompt": "write"}, "output": "draft.md", "approval": "not_required"}
    if typed:
        producer["output_fields"] = [{"name": "markdown", "type": "text"}]
    write_yaml(paths.workflows / f"{wf_id}.yaml", {
        "id": wf_id, "name": wf_id, "status": "active",
        "steps": [producer,
                  {"id": "save", "agent": "writer", "action": "draft_with_model",
                   "input": {"prompt": "polish", "draft": "${draft.md}"},
                   "approval": "not_required"}],
    })


def test_an_untyped_producer_blocks_the_plan(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _agent(paths)
    _two_step(paths, typed=False)
    plan = plan_workflow(load_workflows(paths.workflows)["chain"], load_agents(paths.agents))
    assert plan["can_run"] is False
    producer = next(s for s in plan["steps"] if s["id"] == "draft")
    assert producer["policy"]["status"] == "blocked"
    assert producer["policy"]["permission"] == "workflow.output_contract"
    assert "output_fields" in producer["policy"]["reason"]
    assert "save" in producer["policy"]["reason"]        # names who consumes it


def test_declaring_the_contract_unblocks_it(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _agent(paths)
    _two_step(paths, typed=True)
    plan = plan_workflow(load_workflows(paths.workflows)["chain"], load_agents(paths.agents))
    assert plan["can_run"] is True


def test_an_untyped_model_step_nobody_consumes_is_fine(tmp_path: Path) -> None:
    """The rule is about consumption, not about every model step. A final step
    that writes prose for a human to read needs no contract."""
    paths = init_runtime(tmp_path)
    _agent(paths)
    write_yaml(paths.workflows / "solo.yaml", {
        "id": "solo", "name": "solo", "status": "active",
        "steps": [{"id": "draft", "agent": "writer", "action": "draft_with_model",
                   "input": {"prompt": "write"}, "output": "draft.md", "approval": "not_required"}],
    })
    plan = plan_workflow(load_workflows(paths.workflows)["solo"], load_agents(paths.agents))
    assert plan["can_run"] is True


def test_a_bare_reference_to_an_untyped_output_is_caught_too(tmp_path: Path) -> None:
    """The deprecated form consumes the output just as surely."""
    paths = init_runtime(tmp_path)
    _agent(paths)
    write_yaml(paths.workflows / "bare.yaml", {
        "id": "bare", "name": "bare", "status": "active",
        "steps": [
            {"id": "draft", "agent": "writer", "action": "draft_with_model",
             "input": {"prompt": "write"}, "output": "draft.md", "approval": "not_required"},
            {"id": "save", "agent": "writer", "action": "draft_with_model",
             "input": {"prompt": "p", "draft": "draft.md"}, "approval": "not_required"},
        ],
    })
    plan = plan_workflow(load_workflows(paths.workflows)["bare"], load_agents(paths.agents))
    assert plan["can_run"] is False


def test_a_v2_llm_node_is_held_to_the_same_rule(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _agent(paths)
    write_yaml(paths.workflows / "dag.yaml", {
        "id": "dag", "name": "dag", "status": "active",
        "nodes": [
            {"id": "think", "type": "llm", "agent": "writer",
             "input": {"prompt": "write"}, "output": "draft.md"},
            {"id": "save", "type": "writeback",
             "input": {"path": "workspaces/out.md", "value": "${draft.md}"}},
        ],
        "edges": [{"from": "think", "to": "save"}],
    })
    plan = plan_workflow(load_workflows(paths.workflows)["dag"], load_agents(paths.agents))
    assert plan["can_run"] is False
    think = next(n for n in plan["nodes"] if n["id"] == "think")
    assert think["policy"]["permission"] == "workflow.output_contract"


def test_the_run_refuses_what_the_plan_blocked(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _agent(paths)
    _two_step(paths, typed=False)
    with patch.object(handlers, "call_model", _returns("prose")):
        assert run_workflow(paths, "chain")["status"] == "blocked"


# --- the bundled content ----------------------------------------------------


def test_no_bundled_workflow_consumes_an_untyped_model_output(tmp_path: Path) -> None:
    """The shipped recipes are the reference implementation. `team_launch`
    chained three untyped model steps until this landed."""
    paths = init_runtime(tmp_path, examples=True)
    gaps = [
        f"{wf_id}: {gap['producer']} -> {gap['consumer']}"
        for wf_id, workflow in load_workflows(paths.workflows).items()
        for gap in untyped_model_outputs_consumed(
            list(getattr(workflow, "steps", []) or []) + list(getattr(workflow, "nodes", []) or []))
    ]
    assert gaps == [], f"bundled workflows consuming untyped model output: {gaps}"


def test_the_marketing_example_still_plans_cleanly(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    plan = plan_workflow(load_workflows(paths.workflows)["team_launch"], load_agents(paths.agents))
    blocked = [s["id"] for s in plan["steps"] if s["policy"]["status"] == "blocked"]
    assert blocked == []
    # ...and the two consumed producers carry contracts, while the final step
    # deliberately does not.
    doc = json.loads(json.dumps({s["id"]: s for s in plan["steps"]}))   # plan is JSON-safe
    assert set(doc) == {"core_message", "copy", "review"}

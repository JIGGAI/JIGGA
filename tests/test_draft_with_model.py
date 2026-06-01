from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jigga.commands.init import init_runtime
from jigga.core.config import load_agents
from jigga.core.io import write_yaml
from jigga.core.models import WorkflowStep
from jigga.runtime import handlers
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.dispatcher import RuntimeContext, _draft_prompt, _draft_with_model_handler
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.workflow import run_workflow


def _result(content: str = "", status: str = "ok", error: str | None = None) -> ModelCallResult:
    return ModelCallResult(status=status, provider="dry_run", model="m", content=content, dry_run=True, error=error)


def _echo_user(home, logs_dir, request) -> ModelCallResult:
    """Fake model: echo the user brief so chaining is observable."""
    return _result(content=f"ECHO[{request.items[-1].content}]")


def _writer_agent(paths) -> None:
    write_yaml(paths.agents / "writer.yaml", {
        "id": "writer", "name": "Writer", "role": "Writes copy.",
        "memory_scope": "task_only", "model": "profile:default", "tools": [],
        "permissions": {"network": {"mode": "ask"}, "shell": {"mode": "deny"}},
    })


# --- capability registration ----------------------------------------------


def test_draft_with_model_capability_is_registered(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    reg = CapabilityRegistry.load(user_capabilities=tmp_path / "capabilities", approvals_dir=tmp_path / "policies")
    cap = reg.resolve_action("draft_with_model")
    assert cap is not None
    assert cap.handler == "runtime.draft_with_model"
    assert cap.risk_level == "low"


# --- prompt building -------------------------------------------------------


def test_draft_prompt_string_input(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _writer_agent(paths)
    agent = load_agents(paths.agents)["writer"]
    system, brief = _draft_prompt(agent, "Write a tagline")
    assert "Writer" in system and brief == "Write a tagline"


def test_draft_prompt_dict_input_labels_context(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _writer_agent(paths)
    agent = load_agents(paths.agents)["writer"]
    _system, brief = _draft_prompt(agent, {"prompt": "Improve this", "draft": "old copy here"})
    assert brief.startswith("Improve this")
    assert "## draft" in brief and "old copy here" in brief


# --- handler ---------------------------------------------------------------


def test_handler_returns_model_text(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _writer_agent(paths)
    agent = load_agents(paths.agents)["writer"]
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs, sessions_dir=paths.home / "sessions")
    step = WorkflowStep(id="s1", action="draft_with_model", input="Write a tagline")
    with patch.object(handlers, "call_model", lambda h, lg, r: _result("A bold tagline.")):
        out = _draft_with_model_handler(step, None, "Write a tagline", {}, runtime)
    assert out == "A bold tagline."


def test_handler_raises_on_model_error(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _writer_agent(paths)
    agent = load_agents(paths.agents)["writer"]
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs, sessions_dir=paths.home / "sessions")
    step = WorkflowStep(id="s1", action="draft_with_model", input="x")
    with patch.object(handlers, "call_model", lambda h, lg, r: _result("", status="error", error="boom")):
        with pytest.raises(RuntimeError, match="boom"):
            _draft_with_model_handler(step, None, "x", {}, runtime)


# --- workflow integration (the point of the feature) -----------------------


def test_workflow_chains_model_backed_steps(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _writer_agent(paths)
    write_yaml(paths.workflows / "two_step.yaml", {
        "id": "two_step", "name": "Two Step",
        "steps": [
            {"id": "draft", "agent": "writer", "action": "draft_with_model",
             "input": {"prompt": "Write a tagline"}, "output": "tagline.md", "approval": "not_required"},
            {"id": "polish", "agent": "writer", "action": "draft_with_model",
             "input": {"prompt": "Polish it", "draft": "tagline.md"}, "output": "final.md", "approval": "not_required"},
        ],
    })
    with patch.object(handlers, "call_model", _echo_user):
        result = run_workflow(paths, "two_step")

    assert result["status"] == "completed"
    run_dir = Path(result["run_dir"])
    tagline = (run_dir / "tagline.md").read_text()
    final = (run_dir / "final.md").read_text()
    assert tagline == "ECHO[Write a tagline]"
    # the polish step's brief embedded the draft step's output → chaining works
    assert "ECHO[Write a tagline]" in final
    assert "## draft" in final

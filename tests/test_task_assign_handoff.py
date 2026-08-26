"""task.assign must not lose the delegating agent's handoff.

A lead triaged a ticket, wrote a full brief and a structured handoff, and
called task.assign with them in `task` and `context`. The handler read only
`title` and `description`, so the assignee got a six-word title and an empty
description — and nothing anywhere said the brief had been dropped. The lead
could not have known the field names: task.assign was advertised to the model
as an open object taking anything.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.agent import _parameters_for
from jigga.runtime.capabilities import bundled_capabilities
from jigga.runtime.handlers import _team_orchestration_handler
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.tasks import list_tasks


def _cap():
    return next(c for c in bundled_capabilities() if "task.assign" in c.actions)


def _runtime(paths) -> RuntimeContext:
    agent = AgentConfig(id="lead", name="Lead", role="lead", memory_scope="task_only",
                        tools=["task.assign"], permissions={})
    return RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                          sessions_dir=paths.home / "sessions")


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _assign(paths, payload):
    return _team_orchestration_handler(
        WorkflowStep(id="s", action="task.assign", input={}), _cap(), payload, {}, _runtime(paths))


def test_schema_tells_the_model_the_real_field_names() -> None:
    schema = _parameters_for("task.assign", _cap())
    assert set(schema["properties"]) >= {"assignee", "title", "description", "context"}
    assert set(schema.get("required", [])) >= {"assignee", "title", "description"}


def test_description_and_context_reach_the_assignee(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    handoff = {"requirements": "next.js hello world", "handoff_to": "qa", "acceptance_check_needed": True}
    result = _assign(paths, {"assignee": "dev", "title": "Build it",
                             "description": "Full brief with acceptance check.", "context": handoff})

    task = next(t for t in list_tasks(paths.tasks) if t.id == result["assigned"])
    assert task.description == "Full brief with acceptance check."
    assert task.metadata["context"] == handoff
    assert task.metadata["assigned_by"] == "lead"


def test_unread_fields_are_reported_not_swallowed(tmp_path: Path) -> None:
    """The original failure mode: a brief in a field nothing reads, dropped in silence."""
    paths = init_runtime(tmp_path)
    result = _assign(paths, {"assignee": "dev", "title": "Build it",
                             "task": "the entire brief lived here", "notes": "and here"})

    assert result["ignored_fields"] == ["notes", "task"]
    ignored = [e for e in _events(paths) if e["type"] == "capability.input.ignored"]
    assert ignored, "dropping a field must be audited"


def test_assigning_without_a_brief_is_flagged(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    result = _assign(paths, {"assignee": "dev", "title": "Build it"})

    assert result["description_set"] is False
    assert [e for e in _events(paths) if e["type"] == "task.assigned_without_brief"]


def test_still_requires_assignee_and_title(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    with pytest.raises(ValueError):
        _assign(paths, {"assignee": "dev"})

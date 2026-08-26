"""An agent must be able to name the ticket it is holding.

QA verified a deliverable, reached the right verdict, and then reported: "No
ticket ID was provided, so I could not hand it off with tickets.handoff." It
had the tool, it had the permission, and it had no way to name the thing it was
working. Every tickets.* call takes that id as its first argument, so the board
stalled at QA every lap.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.model_router import ModelCallResult
from jigga.runtime.agent import run_agent
from jigga.runtime.tasks import create_task


def _capture(seen: list[str]):
    def _call(home, logs_dir, request):
        for item in getattr(request, "items", []) or []:
            if getattr(item, "role", None) == "user":
                seen.append(str(getattr(item, "content", "")))
        return ModelCallResult(status="ok", provider="dry_run", model="m",
                               content="done", dry_run=True, tool_calls=[])
    return _call


def _agent(paths, aid="eng-dev") -> None:
    write_yaml(paths.agents / f"{aid}.yaml", {
        "id": aid, "name": aid, "role": "r", "memory_scope": "task_only",
        "tools": [], "permissions": {}, "permission_mode": "autonomous"})


def test_a_board_ticket_tells_the_agent_its_id_and_lane(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _agent(paths)
    write_yaml(paths.home / "teams" / "eng.yaml", {
        "id": "eng", "name": "Eng", "agents": [{"id": "eng-dev", "role": "dev"}],
        "lanes": [{"id": "backlog"}, {"id": "in-progress"}, {"id": "done"}]})
    task = create_task(paths.tasks, "ship it", description="build the thing",
                       assignee="eng-dev", lane="in-progress", metadata={"team_id": "eng"})

    seen: list[str] = []
    with patch("jigga.runtime.agent.call_model", _capture(seen)):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    prompt = "\n".join(seen)
    assert task.id in prompt, "the agent cannot call tickets.handoff without this"
    assert "in-progress" in prompt
    assert "build the thing" in prompt, "the brief must survive the addition"


def test_a_plain_task_is_not_given_a_ticket_header(tmp_path: Path) -> None:
    # No lane means no board; nothing to name.
    paths = init_runtime(tmp_path, examples=True)
    _agent(paths)
    create_task(paths.tasks, "plain", description="just do it", assignee="eng-dev")

    seen: list[str] = []
    with patch("jigga.runtime.agent.call_model", _capture(seen)):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "eng-dev")

    prompt = "\n".join(seen)
    assert "Ticket:" not in prompt
    assert "just do it" in prompt

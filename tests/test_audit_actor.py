"""Every audited event says who did it, and human separates from machine.

On the precursor stack 22 posts vanished and it was permanently unanswerable
who deleted them: the automation wrote through the same API as the humans, and
`created_by` was the constant `dashboard-ui` for every row. The only forensic
tool left was diffing hourly SQLite snapshots to bound the window.

The distinction this pins — `actor_id LIKE 'workflow:%'` cleanly separating
machine from human — is exactly what was missing when 20 unapproved posts
auto-published and nobody could prove which path did it.
"""

from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.audit import (
    ACTOR_SUPERVISOR,
    ACTOR_SYSTEM,
    ACTOR_USER,
    actor_context,
    append_event,
    current_actor,
    is_human,
)
from jigga.runtime.audit_query import format_event, query_events


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


# --- the unit ---------------------------------------------------------------


def test_unattributed_events_are_system_not_blank(tmp_path: Path) -> None:
    """`system` is a real answer meaning 'nothing claimed this'. A blank field
    would read as 'not recorded', which is the state we're leaving behind."""
    append_event(tmp_path, "thing.happened")
    assert json.loads((tmp_path / "events.jsonl").read_text())["actor"] == ACTOR_SYSTEM


def test_actor_context_binds_and_restores(tmp_path: Path) -> None:
    assert current_actor() == ACTOR_SYSTEM
    with actor_context("agent:chief"):
        assert current_actor() == "agent:chief"
        append_event(tmp_path, "agent.did.something")
    assert current_actor() == ACTOR_SYSTEM
    assert json.loads((tmp_path / "events.jsonl").read_text())["actor"] == "agent:chief"


def test_innermost_actor_wins(tmp_path: Path) -> None:
    """A supervisor tick that wakes an agent attributes that agent's actions to
    the agent — that is who performed them. What set it off is recoverable from
    the trace, which is a different question with a different answer."""
    with actor_context(ACTOR_SUPERVISOR):
        with actor_context("agent:chief"):
            append_event(tmp_path, "inner")
        append_event(tmp_path, "outer")
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [e["actor"] for e in events] == ["agent:chief", ACTOR_SUPERVISOR]


def test_is_human_splits_people_from_machinery() -> None:
    assert is_human(ACTOR_USER)
    assert is_human("user:telegram")
    for machine in ("agent:chief", "workflow:weekly", ACTOR_SUPERVISOR, ACTOR_SYSTEM):
        assert not is_human(machine), machine
    # ...and the split can't be spoofed by a lookalike prefix.
    assert not is_human("username-service")


# --- the entry points -------------------------------------------------------


def test_cli_invocations_are_attributed_to_a_person(tmp_path: Path) -> None:
    """Someone typed this. Everything the command does is theirs unless an
    agent or workflow inside takes over. A config change is the clearest case:
    it's a mutation, and it was unambiguously a person."""
    paths = init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "config", "set",
                 "supervisor.max_wakes_per_agent_per_hour", "24"]) == 0
    changed = [e for e in _events(paths) if e["type"] == "config.changed"]
    assert changed, "the config change should have audited something"
    assert changed[-1]["actor"] == ACTOR_USER
    assert is_human(changed[-1]["actor"])


def test_workflow_runs_are_attributed_to_the_workflow(tmp_path: Path, grant) -> None:
    """Even started from the CLI: the steps were executed by the workflow, and
    that is the honest answer to 'who did this'."""
    from jigga.runtime.workflow import run_workflow

    paths = init_runtime(tmp_path, examples=True)
    grant(paths, "daily_briefing_agent", "notifications.send")
    write_yaml(paths.workflows / "wf.yaml", {
        "id": "wf", "name": "wf", "status": "active",
        "steps": [{"id": "ping", "agent": "daily_briefing_agent", "action": "notifications.send",
                   "input": {"message": "hi"}}],
    })
    assert run_workflow(paths, "wf")["status"] == "completed"
    workflow_events = [e for e in _events(paths) if e["type"].startswith("workflow.")]
    assert workflow_events
    assert {e["actor"] for e in workflow_events} == {"workflow:wf"}


def test_supervisor_ticks_are_attributed_to_the_supervisor(tmp_path: Path) -> None:
    from jigga.runtime.supervisor import supervisor_tick

    paths = init_runtime(tmp_path)
    supervisor_tick(paths.home)
    actors = {e["actor"] for e in _events(paths)}
    assert actors, "a tick should have audited something"
    assert ACTOR_USER not in actors        # nothing unattended is ever a person


# --- approvals --------------------------------------------------------------


def test_resolving_an_approval_records_who_did_it(tmp_path: Path) -> None:
    """Approval state used to be derived from status with no actor at all —
    for the one decision whose entire purpose is that a human made it."""
    from jigga.runtime.approvals import request_approval, resolve

    paths = init_runtime(tmp_path)
    code = request_approval(paths.approvals, agent_id="chief", task_id="t1",
                            action="publish")["code"]
    with actor_context("user:telegram"):
        record = resolve(paths.approvals, code, approved=True)
    assert record["resolved_by"] == "user:telegram"
    assert record["resolved_by_human"] is True
    assert record["resolved_at"]


def test_an_approval_resolved_by_machinery_is_flagged_as_such(tmp_path: Path) -> None:
    """If something automated ever resolves an approval, the record says so
    rather than looking identical to a person clicking approve."""
    from jigga.runtime.approvals import request_approval, resolve

    paths = init_runtime(tmp_path)
    code = request_approval(paths.approvals, agent_id="chief", task_id="t1",
                            action="publish")["code"]
    with actor_context("workflow:auto"):
        record = resolve(paths.approvals, code, approved=True)
    assert record["resolved_by"] == "workflow:auto"
    assert record["resolved_by_human"] is False


# --- querying ---------------------------------------------------------------


def _seed(paths) -> None:
    for actor, etype in [(ACTOR_USER, "a"), ("user:telegram", "b"), ("agent:chief", "c"),
                         ("workflow:weekly", "d"), (ACTOR_SUPERVISOR, "e")]:
        with actor_context(actor):
            append_event(paths.logs, etype)


def test_query_by_human_returns_only_people(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _seed(paths)
    assert [e["type"] for e in query_events(paths.logs, actor="human")] == ["a", "b"]


def test_query_by_machine_returns_everything_else(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _seed(paths)
    assert [e["type"] for e in query_events(paths.logs, actor="machine")] == ["c", "d", "e"]


def test_query_by_family_and_by_exact_actor(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _seed(paths)
    assert [e["type"] for e in query_events(paths.logs, actor="user")] == ["a", "b"]
    assert [e["type"] for e in query_events(paths.logs, actor="agent")] == ["c"]
    assert [e["type"] for e in query_events(paths.logs, actor="agent:chief")] == ["c"]
    assert query_events(paths.logs, actor="agent:someone_else") == []


def test_legacy_events_without_an_actor_read_as_system(tmp_path: Path) -> None:
    """Logs written before this existed must still query cleanly rather than
    crashing or silently counting as human."""
    paths = init_runtime(tmp_path)
    (paths.logs / "events.jsonl").write_text(
        json.dumps({"id": "e1", "time": "2026-01-01T00:00:00+00:00", "type": "old",
                    "status": "ok", "details": {}}) + "\n", encoding="utf-8")
    assert [e["type"] for e in query_events(paths.logs, actor="machine")] == ["old"]
    assert query_events(paths.logs, actor="human") == []


def test_the_actor_is_visible_in_the_rendered_line() -> None:
    line = format_event({"time": "2026-08-15T09:00:00+00:00", "type": "workflow.run.completed",
                         "status": "ok", "actor": "workflow:weekly", "details": {"run_id": "r1"}})
    assert "workflow:weekly" in line
    # ...and an event from before the field existed still renders.
    assert "system" in format_event({"time": "t", "type": "old", "status": "ok", "details": {}})


def test_cli_audit_filters_by_actor(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    _seed(paths)
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "audit", "--actor", "human", "--json"]) == 0
    assert [e["type"] for e in json.loads(capsys.readouterr().out)] == ["a", "b"]

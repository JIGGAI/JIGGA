"""Step-input references: explicit and fail-closed, or bare and recorded.

`resolve_value` was `outputs.get(value, value)` — a reference and a literal were
the same thing, so a failed lookup silently became text and was undetectable in
principle. On the precursor stack that ambiguity rendered `{{trigger.skipPublish}}`
as its own template string, failed a truthiness test, and published 20 unapproved
items (FIELD_LESSONS §3.2c).

`${name}` is the fix. Bare names still resolve so nothing breaks on upgrade, but
every one is recorded, which is what makes the remaining uses findable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.dispatcher import UnresolvedReferenceError, resolve_value
from jigga.runtime.workflow import run_workflow


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


# --- the unit ---------------------------------------------------------------


def test_explicit_reference_resolves() -> None:
    assert resolve_value("${draft}", {"draft": "the text"}) == "the text"


def test_explicit_reference_raises_when_it_names_nothing() -> None:
    """The whole point: this used to become the literal string '${draft}'."""
    with pytest.raises(UnresolvedReferenceError) as exc:
        resolve_value("${draft}", {"other": "x"})
    assert "${draft}" in str(exc.value)
    assert "available: other" in str(exc.value)      # says what it could have meant


def test_an_empty_outputs_map_still_explains_itself() -> None:
    with pytest.raises(UnresolvedReferenceError, match="available: none"):
        resolve_value("${anything}", {})


def test_a_reference_resolving_to_a_non_string_keeps_its_type() -> None:
    assert resolve_value("${rows}", {"rows": [1, 2, 3]}) == [1, 2, 3]
    assert resolve_value("${flag}", {"flag": False}) is False


def test_whitespace_around_a_reference_is_tolerated() -> None:
    assert resolve_value("  ${draft}  ", {"draft": "x"}) == "x"
    assert resolve_value("${ draft }", {"draft": "x"}) == "x"


def test_a_string_merely_containing_a_reference_is_a_literal() -> None:
    """Anchored on purpose: a value is a reference or it isn't. There is no
    partial-substitution state where half a string resolved."""
    assert resolve_value("see ${draft} above", {"draft": "x"}) == "see ${draft} above"


def test_bare_names_still_resolve_and_are_recorded() -> None:
    seen: list[str] = []
    assert resolve_value("draft", {"draft": "the text"}, implicit=seen) == "the text"
    assert seen == ["draft"]


def test_a_bare_name_matching_nothing_stays_a_literal() -> None:
    """Unchanged, and exactly why the bare form is being retired: this is
    indistinguishable from a deliberate string."""
    seen: list[str] = []
    assert resolve_value("draft", {}, implicit=seen) == "draft"
    assert seen == []


def test_references_resolve_through_nesting() -> None:
    seen: list[str] = []
    out = resolve_value(
        {"a": "${x}", "b": ["${y}", "bare", "literal"], "c": {"d": "${x}"}},
        {"x": 1, "y": 2, "bare": 3}, implicit=seen)
    assert out == {"a": 1, "b": [2, 3, "literal"], "c": {"d": 1}}
    assert seen == ["bare"]


def test_one_bad_reference_in_a_nested_structure_raises() -> None:
    with pytest.raises(UnresolvedReferenceError, match=r"\$\{missing\}"):
        resolve_value({"ok": "${x}", "bad": ["${missing}"]}, {"x": 1})


# --- through a real workflow ------------------------------------------------


def _wf(paths, steps: list[dict], wf_id: str = "refs") -> None:
    write_yaml(paths.workflows / f"{wf_id}.yaml",
               {"id": wf_id, "name": wf_id, "status": "active", "steps": steps})


def test_an_unresolved_reference_stops_the_run_and_is_audited(tmp_path: Path, grant) -> None:
    """v1 has no per-step catch — a step fault propagates out of `run_workflow`,
    which is how every other failure already behaves there. What matters is that
    the run stops and the audit log names the reference, rather than the step
    proceeding with '${never_produced}' as its literal message."""
    paths = init_runtime(tmp_path, examples=True)
    grant(paths, "daily_briefing_agent", "notifications.send")
    _wf(paths, [{"id": "notify", "agent": "daily_briefing_agent", "action": "notifications.send",
                 "input": {"message": "${never_produced}"}}])
    with pytest.raises(UnresolvedReferenceError, match="never_produced"):
        run_workflow(paths, "refs")
    unresolved = [e for e in _events(paths) if e["type"] == "workflow.reference.unresolved"]
    assert len(unresolved) == 1
    assert unresolved[0]["status"] == "error"
    assert "never_produced" in unresolved[0]["details"]["error"]
    assert unresolved[0]["details"]["step"] == "notify"
    # ...and the send never happened.
    assert not [e for e in _events(paths) if e["type"] == "notification.delivered"]


def test_a_bare_reference_runs_but_leaves_a_record(tmp_path: Path, grant) -> None:
    """Nothing breaks on upgrade — but every remaining use is now findable,
    which is what makes the migration possible at all."""
    paths = init_runtime(tmp_path, examples=True)
    grant(paths, "daily_briefing_agent", "summarize_day", "notifications.send")
    _wf(paths, [
        {"id": "summarize", "agent": "daily_briefing_agent", "action": "summarize_day",
         "input": {}, "output": "summary.md"},
        {"id": "notify", "agent": "daily_briefing_agent", "action": "notifications.send",
         "input": {"message": "summary.md"}},
    ])
    assert run_workflow(paths, "refs")["status"] == "completed"
    implicit = [e for e in _events(paths) if e["type"] == "workflow.reference.implicit"]
    assert len(implicit) == 1
    assert implicit[0]["details"]["reference"] == "summary.md"
    assert "${summary.md}" in implicit[0]["details"]["hint"]


def test_the_explicit_form_runs_clean(tmp_path: Path, grant) -> None:
    paths = init_runtime(tmp_path, examples=True)
    grant(paths, "daily_briefing_agent", "summarize_day", "notifications.send")
    _wf(paths, [
        {"id": "summarize", "agent": "daily_briefing_agent", "action": "summarize_day",
         "input": {}, "output": "summary.md"},
        {"id": "notify", "agent": "daily_briefing_agent", "action": "notifications.send",
         "input": {"message": "${summary.md}"}},
    ])
    assert run_workflow(paths, "refs")["status"] == "completed"
    assert not [e for e in _events(paths) if e["type"] == "workflow.reference.implicit"]


def test_a_v2_writeback_refuses_to_write_its_own_reference_text(tmp_path: Path) -> None:
    """The corruption this exists to prevent: a writeback that silently wrote
    an unsubstituted reference to disk instead of the content it named."""
    from jigga.core.config import load_workflows
    from jigga.runtime.workflow_engine import run_workflow_v2

    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.workflows / "wb.yaml", {
        "id": "wb", "name": "wb", "status": "active",
        "nodes": [{"id": "save", "type": "writeback",
                   "input": {"path": "workspaces/out.md", "value": "${never_produced}"}}],
        "edges": [],
    })
    record = run_workflow_v2(paths, load_workflows(paths.workflows)["wb"])
    assert record["status"] == "failed"
    assert "never_produced" in record["nodes"]["save"]["error"]
    assert not (paths.home / "workspaces" / "out.md").exists()


# --- the bundled content ----------------------------------------------------


def test_no_bundled_workflow_chains_by_bare_name(tmp_path: Path) -> None:
    """The shipped examples are the reference implementation. If they model the
    fail-open form, everyone copies it."""
    from jigga.core.config import load_workflows

    paths = init_runtime(tmp_path, examples=True)
    offenders: list[str] = []
    for wf_id, workflow in load_workflows(paths.workflows).items():
        produced: set[str] = set()
        steps = list(getattr(workflow, "steps", []) or []) + list(getattr(workflow, "nodes", []) or [])
        for step in steps:
            for key, value in (getattr(step, "input", None) or {}).items():
                if isinstance(value, str) and value in produced:
                    offenders.append(f"{wf_id}.{step.id}.{key}={value}")
            if getattr(step, "output", None):
                produced.add(step.output)
            produced.add(step.id)
    assert offenders == [], f"bundled workflows still chaining by bare name: {offenders}"

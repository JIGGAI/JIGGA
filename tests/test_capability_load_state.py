"""Assertions 15 & 16 — a capability that isn't dispatchable must say so.

(2026-06-14) An upgrade changed how the gateway discovered local plugins and
kitchen + recipes silently dropped: the gateway booted with only bundled
plugins and the kitchen's port went dark. Nothing reported it. An empty
capability list is indistinguishable from a working one right up until
something calls an action that isn't there.

(2026-06-01) Separately, `npm install` pruned a manually-created symlink that
was never in the manifest, deleting a live notifications plugin and breaking
all SMS and email for a day. Anything loaded out-of-band from the declared
config is one routine command away from deletion.

JIGGA's shape of both: `~/.jigga/capabilities/<name>/manifest.yaml` is the
install, and `policies/capability_approvals.json` is the declaration. A
capability can therefore be present but not dispatchable three ways, and they
are not equally serious:

  broken     — the manifest won't parse at all
  changed    — approved once, the file differs since (drift, or tampering)
  unapproved — installed, never approved; the routine next step

Before this, the first case raised straight out of `CapabilityRegistry.load`,
taking every other capability down with it, while `jigga doctor` reported a
fully green system.
"""

from __future__ import annotations

from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime import doctor
from jigga.runtime.capabilities import CapabilityRegistry, scan_capability_dir


def _manifest(paths, name: str, *, actions: list[str] | None = None) -> Path:
    directory = paths.capabilities / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.yaml"
    write_yaml(path, {"name": name, "version": "1.0.0", "type": "native",
                      "summary": f"Test capability {name}.",
                      "actions": actions or [f"{name}.do"]})
    return path


def _registry(paths) -> CapabilityRegistry:
    return CapabilityRegistry.load(user_capabilities=paths.capabilities,
                                   approvals_dir=paths.policies)


def _approve(paths, path: Path) -> None:
    """Approve via the production helper, so the fixture can't drift from the
    real format the way a hand-rolled index would."""
    from jigga.runtime.capabilities import load_capability_manifest, record_approval

    record_approval(paths.policies, load_capability_manifest(path))


# --- one bad manifest must not blind the rest -------------------------------


def test_a_broken_manifest_no_longer_aborts_the_whole_registry(tmp_path: Path) -> None:
    """This used to raise out of `load`, so a single unparseable file took every
    capability with it — including the bundled ones that were fine."""
    paths = init_runtime(tmp_path)
    (paths.capabilities / "broken").mkdir(parents=True, exist_ok=True)
    (paths.capabilities / "broken" / "manifest.yaml").write_text("name: broken\nactions: [oops\n")

    registry = _registry(paths)

    assert registry.list(), "bundled capabilities must survive one bad neighbour"
    assert [e.name for e in registry.load_errors] == ["broken"]
    assert "ParserError" in registry.load_errors[0].reason


def test_a_load_error_names_the_file_and_the_reason(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    (paths.capabilities / "wrecked").mkdir(parents=True, exist_ok=True)
    target = paths.capabilities / "wrecked" / "manifest.yaml"
    target.write_text("{[not yaml at all\n")

    error = _registry(paths).load_errors[0]
    assert error.name == "wrecked"           # the directory an operator recognizes
    assert str(target) == error.path
    assert error.reason and len(error.reason) <= 200   # bounded, never a wall of text
    assert error.to_dict()["name"] == "wrecked"


def test_scan_reports_what_loaded_alongside_what_did_not(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _manifest(paths, "good")
    (paths.capabilities / "bad").mkdir(parents=True, exist_ok=True)
    (paths.capabilities / "bad" / "manifest.yaml").write_text("nope: [\n")

    found, errors = scan_capability_dir(paths.capabilities)

    assert [m.name for m in found] == ["good"]
    assert [e.name for e in errors] == ["bad"]


def test_an_empty_capability_dir_is_not_an_error(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    assert scan_capability_dir(paths.capabilities) == ([], [])


# --- unapproved vs changed --------------------------------------------------


def test_a_never_approved_capability_is_pending_as_unapproved(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _manifest(paths, "fresh")

    registry = _registry(paths)

    assert [c.name for c in registry.list_pending()] == ["fresh"]
    assert registry.pending_reasons == {"fresh": "unapproved"}


def test_a_manifest_changed_after_approval_is_distinguished_from_a_new_one(tmp_path: Path) -> None:
    """The severity differs: never-approved is a routine next step, whereas a
    file that changed under an existing approval is drift or tampering."""
    paths = init_runtime(tmp_path)
    path = _manifest(paths, "drifted")
    _approve(paths, path)
    assert _registry(paths).pending_reasons == {}, "approved manifest should be active"

    write_yaml(path, {"name": "drifted", "version": "1.0.0", "type": "native",
                      "summary": "Test capability drifted.",
                      "actions": ["drifted.do", "drifted.something_new"]})

    assert _registry(paths).pending_reasons == {"drifted": "changed"}


def test_the_index_exposes_both_for_machine_consumers(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _manifest(paths, "fresh")
    (paths.capabilities / "bad").mkdir(parents=True, exist_ok=True)
    (paths.capabilities / "bad" / "manifest.yaml").write_text("[\n")

    index = _registry(paths).to_index()

    assert index["pending_reasons"] == {"fresh": "unapproved"}
    assert [e["name"] for e in index["load_errors"]] == ["bad"]


# --- doctor is the loud part ------------------------------------------------


def test_doctor_was_green_on_a_broken_manifest_and_now_fails(tmp_path: Path) -> None:
    """The regression that matters: a capability failing to load reported a
    perfectly healthy system."""
    paths = init_runtime(tmp_path)
    (paths.capabilities / "broken").mkdir(parents=True, exist_ok=True)
    (paths.capabilities / "broken" / "manifest.yaml").write_text("name: broken\nactions: [oops\n")

    check = doctor._check_capabilities(paths)

    assert check.status == doctor.FAIL
    assert "broken" in check.detail
    assert "capabilities" in (check.hint or "")


def test_doctor_fails_on_a_changed_manifest(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    path = _manifest(paths, "drifted")
    _approve(paths, path)
    write_yaml(path, {"name": "drifted", "version": "2.0.0", "type": "native",
                      "summary": "Test capability drifted.", "actions": ["drifted.do"]})

    check = doctor._check_capabilities(paths)

    assert check.status == doctor.FAIL
    assert "changed since approval" in check.detail


def test_doctor_only_warns_on_a_merely_unapproved_capability(tmp_path: Path) -> None:
    """Installing then approving is the normal flow — it must not read as a
    broken system, or the loud states stop being loud."""
    paths = init_runtime(tmp_path)
    _manifest(paths, "fresh")

    check = doctor._check_capabilities(paths)

    assert check.status == doctor.WARN
    assert "not approved" in check.detail
    assert "approve" in (check.hint or "")


def test_doctor_is_green_on_a_clean_runtime(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    check = doctor._check_capabilities(paths)
    assert check.status == doctor.OK
    assert "none pending or broken" in check.detail


def test_the_capabilities_check_runs_as_part_of_the_report(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    (paths.capabilities / "broken").mkdir(parents=True, exist_ok=True)
    (paths.capabilities / "broken" / "manifest.yaml").write_text("[\n")

    report = doctor.run_checks(paths, probe=False)

    capabilities = [c for c in report.checks if c.name == "capabilities"]
    assert len(capabilities) == 1
    assert report.failed, "a capability that cannot load must fail the overall report"


# --- a handler that isn't there ---------------------------------------------


def _ghost(paths, handler: str) -> None:
    """A capability that parses perfectly and names a handler that doesn't exist."""
    directory = paths.capabilities / "ghost"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "manifest.yaml"
    write_yaml(path, {"name": "ghost", "version": "1.0.0", "type": "native",
                      "summary": "Names a handler that does not exist.",
                      "actions": ["ghost.do"], "handler": handler})
    _approve(paths, path)


def test_a_missing_handler_used_to_report_ready(tmp_path: Path) -> None:
    """The gap the parse check missed: this manifest is valid YAML, loads
    cleanly, and was offered to the model as a working tool. It failed only when
    something actually called it."""
    from jigga.core.models import AgentConfig
    from jigga.runtime.dispatcher import effective_tools, unusable_grants

    paths = init_runtime(tmp_path)
    _ghost(paths, "jigga.runtime.does_not_exist:go")
    registry = _registry(paths)
    agent = AgentConfig(id="a", name="A", role="r", tools=["ghost.do"])

    row = effective_tools(agent, registry)[0]
    assert row["status"] == "no_handler"
    assert "not importable" in row["reason"]
    assert [r["action"] for r in unusable_grants(agent, registry)] == ["ghost.do"]


def test_doctor_fails_on_a_capability_with_no_handler(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _ghost(paths, "jigga.runtime.does_not_exist:go")

    check = doctor._check_capabilities(paths)

    assert check.status == doctor.FAIL
    assert "ghost (no handler)" in check.detail


def test_a_handler_that_is_not_a_reference_at_all_is_caught(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _ghost(paths, "totally-made-up")

    from jigga.runtime.dispatcher import handler_problem
    capability = next(c for c in _registry(paths).list() if c.name == "ghost")
    assert "neither a built-in handler" in handler_problem(capability)


def test_the_handler_check_does_not_import_the_module(tmp_path: Path, monkeypatch) -> None:
    """Importing here would execute user-controlled code on every doctor run and
    every tool-list build. `find_spec` answers the question without that."""
    import importlib

    paths = init_runtime(tmp_path)
    _ghost(paths, "jigga.runtime.does_not_exist:go")
    called: list[str] = []
    monkeypatch.setattr(importlib, "import_module",
                        lambda name, *a, **k: called.append(name) or (_ for _ in ()).throw(ImportError(name)))

    doctor._check_capabilities(paths)

    assert called == [], f"handler check imported {called}"


def test_every_bundled_capability_resolves_its_handler() -> None:
    """The regression guard: a shipped capability that names a handler nobody
    registered is a tool the model is offered and can never use."""
    from jigga.runtime.capabilities import bundled_capabilities
    from jigga.runtime.dispatcher import handler_problem

    broken = [(c.name, c.handler, handler_problem(c))
              for c in bundled_capabilities() if handler_problem(c) is not None]
    assert broken == [], f"bundled capabilities with unresolvable handlers: {broken}"

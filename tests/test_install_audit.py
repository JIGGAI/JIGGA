"""Installing a capability is the most privileged thing the CLI does.

It copies a manifest that declares actions, permissions and a handler, then
**auto-approves it** — the approval mechanism that otherwise requires a
deliberate human step. Until now it left no trace at all, so the audit log
could not answer "when did this agent get that power, and who granted it?"

That gap undercut the actor-attribution work: every *dispatch* is attributed,
but the mutation that made the dispatch possible was invisible. The field
lessons make the same point from the other direction — 22 deletions on the
prior-gen stack were permanently unattributable because the mutation wasn't
recorded at the time.

Uninstall matters symmetrically, and is the harder case: once the manifest is
deleted there is nothing left on disk to say what was removed.
"""

from __future__ import annotations

import json
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.commands.install import install_capability, uninstall_capability


def _events(paths, event_type: str) -> list[dict]:
    path = paths.logs / "events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r["type"] == event_type]


def _install(paths, name: str = "daily-brief") -> int:
    return install_capability(paths, name, input_fn=lambda _p: "", print_fn=lambda *_a, **_k: None)


# --- install -----------------------------------------------------------------


def test_installing_records_what_was_granted(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)

    assert _install(paths) == 0

    rows = _events(paths, "capability.installed")
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["capability"] == "daily-brief"
    assert details["actions"]           # the powers this granted
    assert details["auto_approved"] is True
    assert details["reinstall"] is False


def test_the_recorded_hash_is_what_the_approval_is_bound_to(tmp_path: Path) -> None:
    """A later `changed since approval` verdict is only meaningful against a
    known starting point."""
    paths = init_runtime(tmp_path)
    _install(paths)

    from jigga.runtime.capabilities import load_capability_manifest

    installed = load_capability_manifest(paths.capabilities / "daily-brief" / "manifest.yaml")
    assert _events(paths, "capability.installed")[0]["details"]["manifest_hash"] == installed.manifest_hash


def test_the_handler_is_recorded(tmp_path: Path) -> None:
    """The handler is the code an install points new powers at — the single
    most security-relevant field in the manifest."""
    paths = init_runtime(tmp_path)
    _install(paths)
    assert _events(paths, "capability.installed")[0]["details"]["handler"]


def test_a_reinstall_is_distinguishable_from_a_first_install(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _install(paths)
    _install(paths)

    rows = _events(paths, "capability.installed")
    assert [r["details"]["reinstall"] for r in rows] == [False, True]


def test_the_install_is_attributed(tmp_path: Path) -> None:
    """`jigga capabilities install` is run by a person, and the event has to
    say so — an install attributed to nobody is the gap being closed."""
    paths = init_runtime(tmp_path)
    _install(paths)
    assert _events(paths, "capability.installed")[0]["actor"]


# --- uninstall ---------------------------------------------------------------


def test_uninstalling_records_what_was_removed(tmp_path: Path) -> None:
    """The manifest is gone afterwards, so if this isn't captured at the time
    it can never be reconstructed."""
    paths = init_runtime(tmp_path)
    _install(paths)

    assert uninstall_capability(paths, "daily-brief", print_fn=lambda *_a, **_k: None) == 0

    rows = _events(paths, "capability.uninstalled")
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["capability"] == "daily-brief"
    assert details["actions"], "the removed powers must be named while they are still readable"
    assert details["approval_dropped"] is True


def test_uninstalling_something_absent_records_nothing(tmp_path: Path) -> None:
    """A no-op must not manufacture an audit entry."""
    paths = init_runtime(tmp_path)
    assert uninstall_capability(paths, "never-installed", print_fn=lambda *_a, **_k: None) == 1
    assert _events(paths, "capability.uninstalled") == []


def test_an_unreadable_manifest_still_uninstalls_and_records(tmp_path: Path) -> None:
    """Removing a broken capability is exactly when you most want it gone —
    the record degrades, the uninstall doesn't."""
    paths = init_runtime(tmp_path)
    directory = paths.capabilities / "wrecked"
    directory.mkdir(parents=True)
    (directory / "manifest.yaml").write_text("{[not yaml\n")

    assert uninstall_capability(paths, "wrecked", print_fn=lambda *_a, **_k: None) == 0

    rows = _events(paths, "capability.uninstalled")
    assert rows and rows[0]["details"]["capability"] == "wrecked"
    assert rows[0]["details"]["actions"] == []


# --- failure -----------------------------------------------------------------


def test_a_failed_install_is_recorded_and_distinguishable(tmp_path: Path, monkeypatch) -> None:
    """"Never attempted" and "attempted and rolled back" look identical on disk
    — both leave no manifest. Only the log can tell them apart."""
    from jigga.commands import install as install_module

    paths = init_runtime(tmp_path)
    import dataclasses

    # OptionalCapability is frozen, so swap the lookup rather than the record.
    failing = dataclasses.replace(install_module.get_optional("daily-brief"),
                                  setup_fn=lambda *_a, **_k: 3)
    monkeypatch.setattr(install_module, "get_optional", lambda _n: failing)

    assert _install(paths) == 3

    rows = _events(paths, "capability.install_failed")
    assert len(rows) == 1
    assert rows[0]["details"]["exit_code"] == 3
    assert rows[0]["details"]["rolled_back"] is True
    assert _events(paths, "capability.installed") == [], "a failed install must not report success"

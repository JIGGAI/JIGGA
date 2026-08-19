"""Tests cannot reconfigure this machine's services.

Found live, not hypothetically: running the suite rewrote
`~/.config/systemd/user/jigga-supervisor.service` so its ExecStart pointed at
the dev checkout's interpreter. The next `systemctl restart` — which the deploy
does on every core merge — would have moved production off `jigga-stable` and
onto whatever branch happened to be checked out, while the deploy log still
said "core deployed".

The test that did it was not testing systemd. It ran `jigga update --apply` to
check an agent-yaml migration; `update` refreshes the supervisor unit, which is
correct behaviour. The unit path simply ignored `--home`, because systemd only
reads units from the user's real home.

Two guards, deliberately at different layers. The sandbox relocates unit paths
so tests have somewhere harmless to write. The write tripwire catches anything
that reaches a real unit directory by another route — which is how this arrived
the first time, when a guard on `systemctl` was bypassed by the file write that
happens before it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.runtime import service


def test_the_sandbox_is_on_by_default(tmp_path: Path) -> None:
    assert os.environ.get("JIGGA_SERVICE_ROOT"), "conftest must pin this for every test"
    assert Path.home() not in service.systemd_unit_path().parents


def test_installing_a_service_writes_into_the_sandbox(tmp_path: Path) -> None:
    # The end-to-end version: the operation a test would trigger by accident,
    # run on purpose, landing somewhere harmless.
    paths = init_runtime(tmp_path)
    recorded: list[list[str]] = []

    result = service.install_service(paths, interval_seconds=60,
                                     run_fn=lambda argv: recorded.append(argv) or _ok())

    written = Path(result["unit_path"])
    assert written.exists()
    assert Path(os.environ["JIGGA_SERVICE_ROOT"]) in written.parents
    assert Path.home() not in written.parents


def test_the_real_unit_is_never_the_target(tmp_path: Path) -> None:
    real = Path.home() / ".config" / "systemd" / "user" / service.SYSTEMD_UNIT
    assert service.systemd_unit_path() != real


def test_the_tripwire_fires_on_a_write_that_escapes_the_sandbox() -> None:
    """A guard nobody has seen fail is a guard you do not know works.

    This is the write the sandbox exists to prevent, attempted directly — as a
    future hardcoded path would.
    """
    escaped = Path.home() / ".config" / "systemd" / "user" / "jigga-not-a-real-unit.service"
    with pytest.raises(AssertionError, match="tried to write a real service unit"):
        escaped.write_text("[Unit]\n", encoding="utf-8")
    assert not escaped.exists()


@pytest.mark.parametrize("relative", [
    ".config/systemd/user/x.service",
    "Library/LaunchAgents/x.plist",
])
def test_both_backends_real_directories_are_protected(relative: str) -> None:
    with pytest.raises(AssertionError, match="real service unit"):
        (Path.home() / relative).write_text("x", encoding="utf-8")


def test_ordinary_writes_are_untouched(tmp_path: Path) -> None:
    # The tripwire replaces Path.write_text for every test; if it were even
    # slightly too broad it would break the suite in confusing ways.
    target = tmp_path / "notes.md"
    target.write_text("hello", encoding="utf-8")
    assert target.read_text() == "hello"


def _ok():
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

"""Hand-typed runs take the execution lock too.

#210 stopped the supervisor and inline webchat sends from running agents at the
same time. `jigga run agent`, `jigga team run` and `jigga workflow run` never
took the lock, so a command typed while a tick was running could claim and
re-run the work the tick was already doing — claiming a task is a
read-modify-write, and two runners take the same one.

A person typed these, so contention waits briefly rather than refusing outright,
and says who is busy when the wait runs out.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.execution_lock import execution_lock, is_locked


@contextlib.contextmanager
def foreign_holder(home: Path):
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
            from jigga.runtime.execution_lock import execution_lock
            with execution_lock({str(home)!r}) as ok:
                assert ok
                print("held", flush=True)
                time.sleep(60)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        if proc.stdout.readline().strip() != "held":
            pytest.fail("helper never acquired the lock")
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


def _agent(tmp_path: Path, agent_id: str = "solo") -> None:
    write_yaml(tmp_path / "agents" / f"{agent_id}.yaml",
               {"id": agent_id, "name": agent_id, "role": "x", "memory_scope": "task_only",
                "tools": [], "permissions": {}})


def test_a_manual_run_takes_the_lock_when_free(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    _agent(tmp_path)
    assert main(["--home", str(tmp_path), "run", "agent", "solo", "--dry-run-model"]) == 0
    # …and gives it back.
    assert is_locked(tmp_path) is False


def test_a_manual_run_refuses_rather_than_double_running(tmp_path: Path, monkeypatch, capsys) -> None:
    import jigga.cli as cli

    init_runtime(tmp_path)
    _agent(tmp_path)
    monkeypatch.setattr(cli, "MANUAL_RUN_LOCK_WAIT_SECONDS", 0.5)   # don't wait a minute in a test
    with foreign_holder(tmp_path) as holder:
        assert main(["--home", str(tmp_path), "run", "agent", "solo", "--dry-run-model"]) == 1
        err = capsys.readouterr().err
    assert "runtime is busy" in err
    assert str(holder.pid) in err, "say WHO is busy — otherwise there is nothing to check"


def test_it_waits_for_a_short_hold_instead_of_failing(tmp_path: Path, monkeypatch) -> None:
    # The common case is a tick that is nearly finished. Failing a person's
    # command because they typed it half a second early is the wrong answer.
    import threading

    import jigga.cli as cli

    init_runtime(tmp_path)
    _agent(tmp_path)
    monkeypatch.setattr(cli, "MANUAL_RUN_LOCK_WAIT_SECONDS", 10.0)

    released = threading.Event()

    def hold() -> None:
        with execution_lock(tmp_path, blocking=True):
            time.sleep(0.75)
        released.set()

    # A different PROCESS is required for real contention (the lock is
    # re-entrant within one), so hold it from a subprocess and release it.
    proc = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
            from jigga.runtime.execution_lock import execution_lock
            with execution_lock({str(tmp_path)!r}) as ok:
                print("held", flush=True)
                time.sleep(1.0)
        """)],
        stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "held"
    started = time.monotonic()
    assert main(["--home", str(tmp_path), "run", "agent", "solo", "--dry-run-model"]) == 0
    waited = time.monotonic() - started
    proc.wait(timeout=10)
    assert waited >= 0.5, "it should have waited for the holder rather than racing it"


def test_team_run_is_locked_too(tmp_path: Path, monkeypatch, capsys) -> None:
    import jigga.cli as cli

    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "recipes", "scaffold", "development-team",
                 "--id", "eng"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(cli, "MANUAL_RUN_LOCK_WAIT_SECONDS", 0.5)
    with foreign_holder(tmp_path):
        assert main(["--home", str(tmp_path), "team", "run", "eng"]) == 1
    assert "runtime is busy" in capsys.readouterr().err


def test_workflow_run_is_locked_too(tmp_path: Path, monkeypatch, capsys) -> None:
    import jigga.cli as cli

    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "recipes", "scaffold", "development-team",
                 "--id", "eng"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(cli, "MANUAL_RUN_LOCK_WAIT_SECONDS", 0.5)
    with foreign_holder(tmp_path):
        assert main(["--home", str(tmp_path), "workflow", "run", "ship_a_change"]) == 1
    assert "runtime is busy" in capsys.readouterr().err

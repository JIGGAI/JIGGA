"""Only one process runs agents for a home at a time.

The supervisor is a single sequential loop, but it was never the only thing
running agents: `webchat send --wait` ingests inline, `supervisor tick` runs one
by hand, and the deploy timer restarts things underneath both. Two of those
overlapping raced silently — claiming a task is a read-modify-write
(`set_task_state(claimed)` then `running`), so both processes run it, and the
inbox offset is read-then-stored, so both ingest the same message.

The fix is mutual exclusion around execution. These tests use REAL concurrent
processes where it matters: a lock you only test in one process is a lock you
have not tested.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.runtime.execution_lock import execution_lock, holder_pid, is_locked, run_if_free


@contextlib.contextmanager
def foreign_holder(home: Path):
    """Another PROCESS holding the lock.

    Contention has to be simulated across a process boundary now that the lock
    is re-entrant — taking it twice in this process is legal by design, so a
    same-process `with execution_lock(...)` tests nothing about contention.
    """
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
            pytest.fail("the helper process never acquired the lock")
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)


# --- the primitive -----------------------------------------------------------


def test_it_acquires_and_releases(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    with execution_lock(tmp_path) as acquired:
        assert acquired is True
    assert is_locked(tmp_path) is False


def test_nesting_inside_the_same_process_does_not_deadlock_itself(tmp_path: Path) -> None:
    # flock is per open-file-description, so a nested acquisition would open a
    # second fd, fail against its own lock, and report "someone else is
    # running" — a tick would skip its own work. The lock is re-entrant so that
    # is impossible to get wrong.
    init_runtime(tmp_path)
    with execution_lock(tmp_path) as outer:
        assert outer is True
        with execution_lock(tmp_path) as inner:
            assert inner is True
        # …and the inner exit must not have released it for everyone else.
        assert (tmp_path / "state" / "execution.lock").exists()
    assert is_locked(tmp_path) is False


def test_is_locked_means_someone_else(tmp_path: Path) -> None:
    # Holding it yourself is the uninteresting case; a caller inside its own
    # locked block should not be told the runtime is busy with itself.
    init_runtime(tmp_path)
    with execution_lock(tmp_path):
        assert is_locked(tmp_path) is False


def test_the_lock_is_released_when_the_block_raises(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    try:
        with execution_lock(tmp_path) as acquired:
            assert acquired
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert is_locked(tmp_path) is False


def test_a_held_lock_blocks_another_process(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    with foreign_holder(tmp_path) as holder:
        assert is_locked(tmp_path) is True
        assert holder_pid(tmp_path) == holder.pid
    # The kernel releases a flock when the holder dies, so a crashed supervisor
    # leaves nothing to sweep — the reason this is flock and not a lockfile.
    assert is_locked(tmp_path) is False


def test_run_if_free_reports_whether_it_ran(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    ran, result = run_if_free(tmp_path, lambda: "did it")
    assert (ran, result) == (True, "did it")

    with foreign_holder(tmp_path):
        ran, result = run_if_free(tmp_path, lambda: "should not happen")
    assert (ran, result) == (False, None)


# --- the supervisor honours it ----------------------------------------------


def test_a_tick_skips_when_another_process_is_running_agents(tmp_path: Path) -> None:
    from jigga.runtime.supervisor import supervisor_tick

    init_runtime(tmp_path)
    with foreign_holder(tmp_path):
        result = supervisor_tick(tmp_path)
    assert result == {"status": "skipped", "reason": "locked"}


def test_a_skipped_tick_is_audited(tmp_path: Path, capsys) -> None:
    from jigga.runtime.supervisor import supervisor_tick

    init_runtime(tmp_path)
    with foreign_holder(tmp_path):
        supervisor_tick(tmp_path)
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "audit", "--type", "supervisor.tick_skipped",
                 "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 1


def test_a_tick_runs_normally_when_nothing_holds_the_lock(tmp_path: Path) -> None:
    from jigga.runtime.supervisor import supervisor_tick

    init_runtime(tmp_path)
    assert supervisor_tick(tmp_path).get("status") != "skipped"
    # …and it does not leave the lock held for the next caller.
    assert is_locked(tmp_path) is False


# --- webchat defers instead of racing ----------------------------------------


def test_send_wait_answers_inline_when_the_runtime_is_idle(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "webchat", "send", "--wait", "--json",
                 "--text", "hello"]) == 0
    assert json.loads(capsys.readouterr().out)["delivery"] == "answered"


def test_send_wait_queues_instead_of_racing_a_busy_runtime(tmp_path: Path, capsys) -> None:
    # The point of the whole change: a second message while the agent is working
    # is DURABLE immediately and left for whoever holds the lock, rather than
    # starting a second run of the same agent.
    init_runtime(tmp_path)
    with foreign_holder(tmp_path):
        assert main(["--home", str(tmp_path), "webchat", "send", "--wait", "--json",
                     "--text", "queued one"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["delivery"] == "queued"
    assert payload["replies"] == []
    # Durable regardless: the message is in the inbox for the next tick.
    assert payload["message"]["text"] == "queued one"
    inbox = (tmp_path / "channels" / "webchat" / "inbox.jsonl").read_text(encoding="utf-8")
    assert "queued one" in inbox


def test_a_queued_message_survives_for_the_next_tick(tmp_path: Path, capsys) -> None:
    from jigga.runtime.supervisor import supervisor_tick

    init_runtime(tmp_path)
    with foreign_holder(tmp_path):
        main(["--home", str(tmp_path), "webchat", "send", "--wait", "--json", "--text", "later"])
    capsys.readouterr()
    # Nothing consumed it while the lock was held, so the offset has not moved.
    assert supervisor_tick(tmp_path).get("status") != "skipped"
    offset = json.loads((tmp_path / "state" / "webchat_offset.json").read_text())
    assert offset.get("offset", 0) >= 1, "the backstop poll should have consumed the queued message"


def test_the_lock_file_lives_under_state(tmp_path: Path) -> None:
    # Somewhere a human looking for "why is nothing running" would think to look,
    # next to the other runtime state — not /tmp, which a reboot silently clears.
    init_runtime(tmp_path)
    with execution_lock(tmp_path):
        assert (tmp_path / "state" / "execution.lock").exists()
        assert holder_pid(tmp_path) == os.getpid()

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


def _agent_with_a_pending_task(tmp_path: Path) -> None:
    from jigga.core.io import write_yaml
    from jigga.runtime.tasks import create_task

    write_yaml(tmp_path / "agents" / "worker.yaml",
               {"id": "worker", "name": "Worker", "role": "x", "memory_scope": "task_only",
                "tools": [], "permissions": {}})
    create_task(tmp_path / "tasks", "do a thing", None, "worker", None)


def test_a_contended_tick_defers_the_agents_but_still_ticks(tmp_path: Path) -> None:
    # It used to skip the WHOLE tick. Log rotation, recovery sweeps and channel
    # polling are not execution and have no reason to stop because something
    # else is running an agent.
    from jigga.runtime.supervisor import supervisor_tick
    from jigga.runtime.tasks import list_tasks

    init_runtime(tmp_path)
    _agent_with_a_pending_task(tmp_path)
    with foreign_holder(tmp_path):
        result = supervisor_tick(tmp_path)
    assert result.get("status") != "skipped"
    assert result.get("runs") in (None, [])
    # Nothing is lost: the task is still pending for whoever holds the lock.
    assert [t.state for t in list_tasks(tmp_path / "tasks")] == ["pending"]


def test_deferred_wakes_are_audited(tmp_path: Path, capsys) -> None:
    from jigga.runtime.supervisor import supervisor_tick

    init_runtime(tmp_path)
    _agent_with_a_pending_task(tmp_path)
    with foreign_holder(tmp_path):
        supervisor_tick(tmp_path)
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "audit", "--type", "supervisor.wake_deferred",
                 "--json"]) == 0
    events = json.loads(capsys.readouterr().out)
    assert len(events) == 1 and events[0]["details"]["agents"] == ["worker"]


def test_an_uncontended_tick_runs_its_agents(tmp_path: Path) -> None:
    from jigga.runtime.supervisor import supervisor_tick

    init_runtime(tmp_path)
    _agent_with_a_pending_task(tmp_path)
    assert supervisor_tick(tmp_path)["runs"], "an idle runtime should run the pending task"
    # …and it does not leave the lock held for the next caller.
    assert is_locked(tmp_path) is False


def test_the_lock_is_not_held_across_a_channel_poll(tmp_path: Path, monkeypatch) -> None:
    """The regression that made every first chat message wait a full tick.

    With a channel enabled the tick spends ~30s inside a long-poll. Holding the
    execution lock across that made the runtime look permanently busy: a
    browser send found the lock taken every time and queued for the next tick,
    so an instant reply became a 60-second one. The poll is a network WAIT, not
    execution, and must happen outside the lock.
    """
    from jigga.runtime import channel_listener

    init_runtime(tmp_path)
    seen: list[bool] = []

    class SlowAdapter:
        long_polls = True

        def poll(self, home, long_poll_seconds=0):
            # What a concurrent `webchat send --wait` would observe right now.
            seen.append(is_locked(home))
            return {"status": "ok", "events": []}

        def send(self, home, **kwargs):
            return {"ok": True}

    monkeypatch.setitem(channel_listener.ADAPTERS, "webchat", SlowAdapter())
    monkeypatch.setattr(channel_listener, "enabled_channels",
                        lambda home: [("webchat", {"activation": "always", "enabled": True})])
    channel_listener.ingest_once(tmp_path, tmp_path / "logs", tmp_path / "tasks",
                                 tmp_path / "agents", long_poll_seconds=30)
    assert seen == [False], "the runtime must look idle while it is only listening"


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

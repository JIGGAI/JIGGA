"""One process runs agents for a given home at a time.

Nothing enforced this. The supervisor is a single sequential loop, but it is
not the only thing that runs agents: `jigga webchat send --wait` ingests inline
so browser chat feels synchronous, `jigga supervisor tick` runs a tick by hand,
and `jigga run agent` runs one directly. Any two of those overlapping raced,
and the races were silent rather than loud:

  - claiming is a read-modify-write (`set_task_state(claimed)` then `running`),
    so two processes both see a pending task and both run it — the agent answers
    twice, bills twice, and appends its output twice
  - `poll_messages` reads the inbox slice, then stores the advanced offset, so
    two consumers interleaved between those two steps ingest the same message

Both need mutual exclusion around *execution*, not a smarter data structure —
the file layout is fine, the concurrency was unmanaged. `flock` is the right
primitive here: it is released by the kernel when the holder exits, so a
crashed supervisor leaves no stale lock to sweep (the failure mode that makes
lockfile-based schemes worse than the problem).

Non-blocking by design. A caller that cannot get the lock has not failed — the
work is already queued on disk, and whoever holds the lock will get to it. That
is what makes a "nudge" safe: it runs the ingest now IF nothing else is
running, and otherwise does nothing at all.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


def lock_path(home: Path) -> Path:
    return Path(home) / "state" / "execution.lock"


# Homes this process already holds, by depth. flock is per open-file-description,
# so a nested acquisition would open a SECOND fd, fail to lock against itself,
# and report "someone else is running" — a tick would then skip its own work.
# Re-entrancy makes that impossible to get wrong: the outermost caller owns the
# lock, inner ones ride along.
_HELD: dict[str, int] = {}


@contextmanager
def execution_lock(home: Path, *, blocking: bool = False) -> Iterator[bool]:
    """Yield True if this process holds the execution lock, False if another does.

    Callers decide what "someone else is running" means for them; nobody gets an
    exception, because contention is the normal case, not an error.
    """
    path = lock_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)

    key = str(path.resolve())
    if _HELD.get(key):
        _HELD[key] += 1
        try:
            yield True
        finally:
            _HELD[key] -= 1
        return

    if fcntl is None:  # pragma: no cover - Windows has no flock
        # Better to run unlocked than to refuse to run at all; Windows support
        # is a separate piece of work (msvcrt.locking has different semantics).
        yield True
        return

    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(handle, flags)
        except OSError:
            yield False
            return
        # Who holds it, for `jigga doctor` and for a human reading the file.
        os.ftruncate(handle, 0)
        os.write(handle, f"{os.getpid()}\n".encode())
        _HELD[key] = 1
        try:
            yield True
        finally:
            _HELD.pop(key, None)
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)


def holder_pid(home: Path) -> int | None:
    """The pid recorded in the lock file, or None. Advisory only — the file
    outlives the lock, so a pid here does NOT mean the lock is held."""
    try:
        text = lock_path(home).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def is_locked(home: Path) -> bool:
    """Whether ANOTHER process currently holds the lock.

    Not "is the lock held" — this process holding it is the uninteresting case,
    and with re-entrancy a caller inside its own locked block would otherwise
    be told the runtime is busy with itself.
    """
    if _HELD.get(str(lock_path(home).resolve())):
        return False
    with execution_lock(home) as acquired:
        return not acquired


def run_if_free(home: Path, work: Any) -> tuple[bool, Any]:
    """Run `work()` under the lock, or report that someone else is running.

    Returns (ran, result). This is the whole contract a nudge needs: do it now
    if the runtime is idle, otherwise leave it to whoever is already working.
    """
    with execution_lock(home) as acquired:
        if not acquired:
            return False, None
        return True, work()

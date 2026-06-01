from __future__ import annotations

import signal
import time
from collections import deque
from pathlib import Path
from typing import Any

from jigga.runtime.audit import append_event
from jigga.runtime.supervisor import supervisor_tick

# An always-on supervisor runs indefinitely; keep only the most recent tick
# summaries in memory (the audit log holds the durable, full record).
_TICK_HISTORY = 100


def supervisor_loop(
    home: str | Path | None = None,
    interval_seconds: float = 60,
    max_ticks: int | None = None,
) -> dict[str, Any]:
    stopped = {"flag": False, "signal": None}

    def _handle(signum: int, _frame: Any) -> None:
        stopped["flag"] = True
        stopped["signal"] = signum

    # signal.signal() only works on the main thread; fall back to no-handler
    # behavior (still works for max_ticks-bounded loops in tests) elsewhere.
    previous_int = previous_term = None
    try:
        previous_int = signal.signal(signal.SIGINT, _handle)
        previous_term = signal.signal(signal.SIGTERM, _handle)
    except (ValueError, OSError):
        pass

    ticks: deque[dict[str, Any]] = deque(maxlen=_TICK_HISTORY)
    count = 0
    try:
        while not stopped["flag"] and (max_ticks is None or count < max_ticks):
            result = supervisor_tick(home)
            ticks.append(result)
            count += 1
            if stopped["flag"] or (max_ticks is not None and count >= max_ticks):
                break
            # time.sleep is interruptible by signals on POSIX; the signal handler
            # runs, sets the flag, and sleep returns early.
            time.sleep(interval_seconds)
    finally:
        if previous_int is not None:
            try:
                signal.signal(signal.SIGINT, previous_int)
            except (ValueError, OSError):
                pass
        if previous_term is not None:
            try:
                signal.signal(signal.SIGTERM, previous_term)
            except (ValueError, OSError):
                pass

    return {
        "status": "interrupted" if stopped["flag"] else "stopped",
        "stopped_by_signal": stopped["signal"],
        "tick_count": count,
        "ticks": list(ticks),
    }


def record_supervisor_start(logs_dir: Path, interval_seconds: float, max_ticks: int | None) -> None:
    append_event(logs_dir, "supervisor.start", interval_seconds=interval_seconds, max_ticks=max_ticks)

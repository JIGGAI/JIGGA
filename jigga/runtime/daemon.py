from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from jigga.runtime.audit import append_event
from jigga.runtime.supervisor import supervisor_tick


def supervisor_loop(
    home: str | Path | None = None,
    interval_seconds: float = 60,
    max_ticks: int | None = None,
) -> dict[str, Any]:
    ticks: list[dict[str, Any]] = []
    count = 0
    while max_ticks is None or count < max_ticks:
        result = supervisor_tick(home)
        ticks.append(result)
        count += 1
        if max_ticks is not None and count >= max_ticks:
            break
        time.sleep(interval_seconds)
    return {"status": "stopped", "tick_count": count, "ticks": ticks}


def record_supervisor_start(logs_dir: Path, interval_seconds: float, max_ticks: int | None) -> None:
    append_event(logs_dir, "supervisor.start", interval_seconds=interval_seconds, max_ticks=max_ticks)

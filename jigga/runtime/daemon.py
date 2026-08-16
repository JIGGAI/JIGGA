from __future__ import annotations

import signal
import time
from collections import deque
from pathlib import Path
from typing import Any

from jigga.core.paths import get_paths
from jigga.runtime.audit import append_event
from jigga.runtime.channel_listener import DEFAULT_LONG_POLL_SECONDS, long_polling_channels_enabled
from jigga.runtime.supervisor import supervisor_tick

# An always-on supervisor runs indefinitely; keep only the most recent tick
# summaries in memory (the audit log holds the durable, full record).
_TICK_HISTORY = 100


def supervisor_loop(
    home: str | Path | None = None,
    interval_seconds: float = 60,
    max_ticks: int | None = None,
    channel_long_poll_seconds: int = DEFAULT_LONG_POLL_SECONDS,
) -> dict[str, Any]:
    """Run supervisor ticks until interrupted.

    When a chat channel is enabled, each tick long-polls it (blocking up to
    `channel_long_poll_seconds`, returning the instant a message arrives), and
    the loop runs ticks back-to-back with no inter-tick sleep — so inbound
    messages are handled in near-real-time instead of waiting up to
    `interval_seconds` for the next cron tick. With no channel enabled the loop
    keeps the classic cadence: one tick, then sleep `interval_seconds`."""
    paths = get_paths(home)
    home_path = paths.home
    stopped = {"flag": False, "signal": None, "at": None}

    def _handle(signum: int, _frame: Any) -> None:
        stopped["flag"] = True
        stopped["signal"] = signum
        stopped["at"] = time.monotonic()
        # Written from a signal handler deliberately: if the drain is cut short
        # by a SIGKILL escalation, this is the only record that a clean stop was
        # ever attempted — which is the difference between "orphaned claim,
        # cause unknown" and "the stop timeout is too short".
        try:
            append_event(paths.logs, "supervisor.draining", status="ask", signal=signum,
                         note="finishing the current tick before exiting")
        except Exception:  # noqa: BLE001 — a logging fault must not stop the stop
            pass

    # signal.signal() only works on the main thread; fall back to no-handler
    # behavior (still works for max_ticks-bounded loops in tests) elsewhere.
    previous_int = previous_term = None
    try:
        previous_int = signal.signal(signal.SIGINT, _handle)
        previous_term = signal.signal(signal.SIGTERM, _handle)
    except (ValueError, OSError):
        pass

    # Optional inbound listener. It only authenticates and enqueues; the drain
    # in the tick below is what actually runs anything, so it inherits the tick
    # budget and the opt-in targeting check rather than having its own.
    webhook = None
    try:
        from jigga.runtime.webhook import serve_in_background

        webhook = serve_in_background(paths)
    except Exception as exc:  # noqa: BLE001 — the supervisor runs agents with or without it
        append_event(paths.logs, "webhook.not_started", status="error", error=str(exc))

    ticks: deque[dict[str, Any]] = deque(maxlen=_TICK_HISTORY)
    count = 0
    try:
        while not stopped["flag"] and (max_ticks is None or count < max_ticks):
            # Re-checked each tick so enabling/disabling a channel takes effect
            # without restarting the daemon. Only a channel whose poll actually
            # blocks (Telegram long-poll) counts: it paces the loop, so ticks
            # can run back-to-back. Webchat polls a local file and returns
            # instantly — if it counted, the loop would hot-spin; it keeps the
            # cron cadence instead (the `webchat send --wait` path is what
            # makes browser chat real-time, not the supervisor).
            channels_on = long_polling_channels_enabled(home_path)
            poll_seconds = channel_long_poll_seconds if channels_on else 0
            result = supervisor_tick(home, channel_long_poll_seconds=poll_seconds)
            ticks.append(result)
            count += 1
            if stopped["flag"] or (max_ticks is not None and count >= max_ticks):
                break
            # With channels on, the tick already blocked in a long-poll (which
            # returns immediately on a message), so it paces the loop — no extra
            # sleep, keeping replies near-real-time. Otherwise keep the cron
            # cadence. time.sleep is interruptible by signals on POSIX; the
            # handler runs, sets the flag, and sleep returns early.
            time.sleep(0 if channels_on else interval_seconds)
    finally:
        if webhook is not None:
            # Stop accepting before the drain finishes: anything already queued
            # is durable on disk, but taking new work while shutting down would
            # leave it for a restart that may be a different version.
            server, _thread = webhook
            try:
                server.shutdown()
                server.server_close()
                append_event(paths.logs, "webhook.stopped")
            except Exception:  # noqa: BLE001 — shutdown must not mask the real exit
                pass
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

    drain_seconds = None
    if stopped["flag"] and stopped["at"] is not None:
        drain_seconds = round(time.monotonic() - stopped["at"], 1)
        # Pairs with `supervisor.draining`. Present = the drain completed and
        # nothing was orphaned; absent = the init system killed us first, and
        # `drain_seconds` on the next successful stop tells you how much
        # TimeoutStopSec headroom the tick actually needs.
        try:
            append_event(paths.logs, "supervisor.drained", signal=stopped["signal"],
                         drain_seconds=drain_seconds, ticks=count)
        except Exception:  # noqa: BLE001
            pass

    return {
        "status": "interrupted" if stopped["flag"] else "stopped",
        "stopped_by_signal": stopped["signal"],
        "drain_seconds": drain_seconds,
        "tick_count": count,
        "ticks": list(ticks),
    }


def record_supervisor_start(logs_dir: Path, interval_seconds: float, max_ticks: int | None) -> None:
    append_event(logs_dir, "supervisor.start", interval_seconds=interval_seconds, max_ticks=max_ticks)

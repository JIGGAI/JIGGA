"""Cross-platform desktop notification delivery.

Replaces the original dry-run `notifications.send` capability handler with a
real adapter so the bundled `morning_day_summary` / `meeting_reminders`
workflows actually pop up on the user's desktop.

Platform support:
  - Linux: `notify-send` (libnotify) if available.
  - macOS: `osascript` driving the AppleScript `display notification` verb.
  - Windows: not yet wired; returns a structured `unsupported` result so the
    caller can fall back / log without crashing. The first Windows user can
    add a PowerShell + WinRT toast handler behind the same shape.

The shared `runtime.sandbox` primitive is intentionally NOT used here. These
helpers are local-UX tools, not bounded external CLIs: we want them to inherit
the user's locale, X display, audio device, and notification daemon socket,
which are all in the env we'd otherwise strip. The audit log captures the
delivery decision; sandboxing the spawn would buy nothing.

Test-mode override: setting `JIGGA_NOTIFICATION_MODE=dry_run` in the
environment forces dry-run regardless of runtime config. Pytest uses this via
conftest.py so unit tests never actually pop a desktop notification.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jigga.core.config import load_runtime_config

VALID_URGENCIES = ("low", "normal", "high", "critical")
ENV_OVERRIDE = "JIGGA_NOTIFICATION_MODE"
DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class NotificationRequest:
    title: str
    body: str
    urgency: str = "normal"


@dataclass(frozen=True)
class NotificationResult:
    delivered: bool
    backend: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def delivery_mode(home: Path) -> str:
    """Resolve the active delivery mode.

    Order: `JIGGA_NOTIFICATION_MODE` env var (test-mode override) →
    `notifications.delivery_mode` in `~/.jigga/config.yaml` → default `"real"`.
    """
    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return override.strip().lower() or "real"
    config = load_runtime_config(home)
    notifications = config.get("notifications") or {}
    return str(notifications.get("delivery_mode") or "real").lower()


def _escape_applescript(text: str) -> str:
    """Escape user-supplied text for `osascript -e "..."`.

    AppleScript strings use double quotes and backslash escapes. We escape
    backslashes first (so we don't double-escape the escapes we add next),
    then double quotes, then collapse newlines because AppleScript's
    `display notification` truncates at the first line break anyway.
    """
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def _send_linux(req: NotificationRequest) -> NotificationResult:
    if not shutil.which("notify-send"):
        return NotificationResult(
            delivered=False,
            backend="linux",
            error="notify-send not installed (apt install libnotify-bin / pacman -S libnotify)",
        )
    urgency_map = {"low": "low", "normal": "normal", "high": "critical", "critical": "critical"}
    cmd = [
        "notify-send",
        "--app-name=JIGGA",
        f"--urgency={urgency_map.get(req.urgency, 'normal')}",
        req.title,
        req.body,
    ]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=DEFAULT_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return NotificationResult(delivered=False, backend="notify-send", error="notify-send timed out")
    if completed.returncode != 0:
        return NotificationResult(
            delivered=False,
            backend="notify-send",
            error=(completed.stderr or completed.stdout or f"exit_code={completed.returncode}").strip()[:500],
        )
    return NotificationResult(delivered=True, backend="notify-send")


def _send_macos(req: NotificationRequest) -> NotificationResult:
    if not shutil.which("osascript"):
        return NotificationResult(delivered=False, backend="darwin", error="osascript not available")
    script = (
        f'display notification "{_escape_applescript(req.body)}" '
        f'with title "{_escape_applescript(req.title)}"'
    )
    try:
        completed = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return NotificationResult(delivered=False, backend="osascript", error="osascript timed out")
    if completed.returncode != 0:
        return NotificationResult(
            delivered=False,
            backend="osascript",
            error=(completed.stderr or completed.stdout or f"exit_code={completed.returncode}").strip()[:500],
        )
    return NotificationResult(delivered=True, backend="osascript")


def _send_unsupported(req: NotificationRequest, system: str) -> NotificationResult:
    return NotificationResult(
        delivered=False,
        backend=f"unsupported-{system.lower()}",
        error=f"Native notification delivery is not yet implemented on {system}.",
    )


def send_notification(
    request: NotificationRequest,
    *,
    dry_run: bool = False,
) -> NotificationResult:
    """Deliver a desktop notification.

    When `dry_run` is True the function returns immediately without spawning
    any subprocess — used by tests and by workflows running under a dry-run
    notification config.
    """
    if dry_run:
        return NotificationResult(delivered=False, backend="dry_run")
    system = platform.system()
    if system == "Linux":
        return _send_linux(request)
    if system == "Darwin":
        return _send_macos(request)
    return _send_unsupported(request, system or "unknown")

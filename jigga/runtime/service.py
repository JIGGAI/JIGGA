"""Install the always-on supervisor as an OS-managed user service.

`jigga supervisor start` runs the supervisor loop in the foreground — fine for
a terminal, but it dies on logout/reboot, so the "always-on" promise only holds
while you babysit it. This module registers the loop as a **user-level** service
that the OS keeps running and restarts on crash:

  - macOS  → a launchd LaunchAgent in ~/Library/LaunchAgents (per-user, no sudo)
  - Linux  → a systemd **user** unit in ~/.config/systemd/user (no root)

Design notes:
- The service runs `<python> -m jigga supervisor start` (not the `jigga` console
  script), so it works regardless of whether `~/.local/bin` or the venv is on
  the service's minimal PATH. `<python>` is the interpreter that installed the
  service (`sys.executable`) — i.e. the venv JIGGA lives in.
- `JIGGA_HOME` is pinned into the unit's environment so the service targets the
  same runtime the installer used, even under `--home`/a non-default home.
- Pure rendering (`render_launchd_plist` / `render_systemd_unit`,
  `service_argv`) is split from the side-effecting install so it's unit-testable
  without touching the real launchd/systemd. The install/uninstall/status
  functions take an injectable `run_fn` and a `dry_run` flag for the same reason.
- On an unsupported platform (or in a container without a user service manager)
  install returns `backend="unsupported"` with manual run instructions rather
  than failing — the foreground `jigga supervisor start` always works.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from jigga.core.paths import JiggaPaths

LAUNCHD_LABEL = "ai.jigga.supervisor"
SYSTEMD_UNIT = "jigga-supervisor.service"

RunFn = Callable[[list[str]], subprocess.CompletedProcess]


def _default_run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def detect_backend() -> str:
    """Return the available user-service backend: ``launchd`` (macOS),
    ``systemd`` (Linux with the ``systemctl`` user bus), else ``unsupported``."""
    system = platform.system()
    if system == "Darwin":
        return "launchd" if shutil.which("launchctl") else "unsupported"
    if system == "Linux":
        return "systemd" if shutil.which("systemctl") else "unsupported"
    return "unsupported"


def service_argv(python: str, interval_seconds: float) -> list[str]:
    """The argv the service runs: the supervisor loop via ``python -m jigga``."""
    return [python, "-m", "jigga", "supervisor", "start",
            "--interval-seconds", _fmt_interval(interval_seconds)]


def _fmt_interval(interval_seconds: float) -> str:
    # 60.0 -> "60" but 2.5 -> "2.5"; argparse parses either as float.
    return str(int(interval_seconds)) if float(interval_seconds).is_integer() else str(interval_seconds)


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def _xml_escape(value: str) -> str:
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def render_launchd_plist(argv: list[str], home: Path, logs_dir: Path) -> str:
    args_xml = "\n".join(f"    <string>{_xml_escape(a)}</string>" for a in argv)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args_xml}
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>JIGGA_HOME</key>
    <string>{_xml_escape(str(home))}</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>{_xml_escape(str(home))}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{_xml_escape(str(logs_dir / "supervisor.out.log"))}</string>
  <key>StandardErrorPath</key>
  <string>{_xml_escape(str(logs_dir / "supervisor.err.log"))}</string>
</dict>
</plist>
"""


def render_systemd_unit(argv: list[str], home: Path) -> str:
    exec_start = " ".join(argv)
    return f"""[Unit]
Description=JIGGA supervisor (always-on agent scheduler)
After=network-online.target

[Service]
Type=simple
Environment=JIGGA_HOME={home}
WorkingDirectory={home}
ExecStart={exec_start}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def _manual_instructions(argv: list[str]) -> str:
    cmd = " ".join(argv)
    return ("No user service manager (launchd/systemd) was found. Run the "
            f"supervisor in the foreground or under your own process manager:\n  {cmd}")


def install_service(
    paths: JiggaPaths,
    *,
    interval_seconds: float = 60.0,
    python: str | None = None,
    dry_run: bool = False,
    run_fn: RunFn = _default_run,
) -> dict:
    """Write the user-service unit and register it so it starts now and on login.

    Returns a structured result: ``backend``, ``unit_path``, the ``argv`` the
    service runs, the control ``commands`` executed (or that would be, when
    ``dry_run``), whether each succeeded, and any ``instructions`` for the
    unsupported case. Never raises on a control-tool failure — surfaces it in
    the result so the caller can show the unit path for manual loading.
    """
    backend = detect_backend()
    py = python or sys.executable
    argv = service_argv(py, interval_seconds)
    result: dict = {"backend": backend, "argv": argv, "dry_run": dry_run, "commands": []}

    if backend == "unsupported":
        result["instructions"] = _manual_instructions(argv)
        return result

    if backend == "launchd":
        unit_path = launchd_plist_path()
        content = render_launchd_plist(argv, paths.home, paths.logs)
        domain = f"gui/{os.getuid()}"
        target = f"{domain}/{LAUNCHD_LABEL}"
        commands = [
            ["launchctl", "bootout", domain, str(unit_path)],   # clear a prior load (ok to fail)
            ["launchctl", "bootstrap", domain, str(unit_path)],
            ["launchctl", "enable", target],
            ["launchctl", "kickstart", "-k", target],
        ]
        optional_first = True  # the bootout may legitimately fail when not yet loaded
    else:  # systemd
        unit_path = systemd_unit_path()
        content = render_systemd_unit(argv, paths.home)
        commands = [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT],
        ]
        optional_first = False

    result["unit_path"] = str(unit_path)
    result["unit_content"] = content
    if dry_run:
        result["commands"] = [{"argv": c, "ran": False} for c in commands]
        return result

    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(content, encoding="utf-8")
    paths.logs.mkdir(parents=True, exist_ok=True)

    started = True
    for i, cmd in enumerate(commands):
        proc = run_fn(cmd)
        ok = proc.returncode == 0
        entry = {"argv": cmd, "ran": True, "returncode": proc.returncode}
        if not ok:
            entry["stderr"] = (proc.stderr or "").strip()
        result["commands"].append(entry)
        # A failing leading bootout (launchd, not-yet-loaded) is expected; only
        # treat the remaining steps as load-determining.
        if not ok and not (optional_first and i == 0):
            started = False
    result["started"] = started
    return result


def uninstall_service(
    paths: JiggaPaths,
    *,
    dry_run: bool = False,
    run_fn: RunFn = _default_run,
) -> dict:
    """Stop and remove the user service (no-op-safe if it was never installed)."""
    backend = detect_backend()
    result: dict = {"backend": backend, "dry_run": dry_run, "commands": []}
    if backend == "launchd":
        unit_path = launchd_plist_path()
        commands = [["launchctl", "bootout", f"gui/{os.getuid()}", str(unit_path)]]
    elif backend == "systemd":
        unit_path = systemd_unit_path()
        commands = [
            ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT],
            ["systemctl", "--user", "daemon-reload"],
        ]
    else:
        result["instructions"] = "No user service manager found; nothing to remove."
        return result

    result["unit_path"] = str(unit_path)
    if dry_run:
        result["commands"] = [{"argv": c, "ran": False} for c in commands]
        result["removed"] = unit_path.exists()
        return result

    for cmd in commands:
        proc = run_fn(cmd)
        result["commands"].append({"argv": cmd, "ran": True, "returncode": proc.returncode})
    removed = False
    if unit_path.exists():
        unit_path.unlink()
        removed = True
    result["removed"] = removed
    return result


def status_service(paths: JiggaPaths, *, run_fn: RunFn = _default_run) -> dict:
    """Report whether the service is installed (unit file present) and what the
    OS reports for its run state."""
    backend = detect_backend()
    result: dict = {"backend": backend}
    if backend == "launchd":
        unit_path = launchd_plist_path()
        result["installed"] = unit_path.exists()
        result["unit_path"] = str(unit_path)
        proc = run_fn(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
        result["running"] = proc.returncode == 0
        result["detail"] = (proc.stdout or proc.stderr or "").strip()[:2000]
    elif backend == "systemd":
        unit_path = systemd_unit_path()
        result["installed"] = unit_path.exists()
        result["unit_path"] = str(unit_path)
        proc = run_fn(["systemctl", "--user", "is-active", SYSTEMD_UNIT])
        state = (proc.stdout or "").strip()
        result["running"] = state == "active"
        result["detail"] = state or (proc.stderr or "").strip()
    else:
        result["installed"] = False
        result["instructions"] = "No user service manager found on this platform."
    return result

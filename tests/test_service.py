"""Tests for the supervisor autostart service layer (jigga/runtime/service.py).

The OS-touching parts (launchctl/systemctl, the real LaunchAgents/systemd dirs)
are isolated by monkeypatching the backend detector + unit-path helpers and
injecting a fake ``run_fn``, so these run identically on any CI host.
"""

from __future__ import annotations

import subprocess

from jigga.core.paths import get_paths
from jigga.runtime import service


def _proc(argv, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr=stderr)


def _recorder(codes=None):
    """A fake run_fn that records calls and returns a code per call index
    (default 0). ``codes`` maps call-index -> returncode."""
    codes = codes or {}
    calls = []

    def run_fn(argv):
        idx = len(calls)
        calls.append(argv)
        return _proc(argv, returncode=codes.get(idx, 0))

    run_fn.calls = calls
    return run_fn


# ---- pure helpers -----------------------------------------------------------

def test_service_argv_runs_supervisor_via_module():
    argv = service.service_argv("/venv/bin/python", 60)
    assert argv == ["/venv/bin/python", "-m", "jigga", "supervisor", "start",
                    "--interval-seconds", "60"]


def test_interval_formats_int_and_float():
    assert service.service_argv("py", 60.0)[-1] == "60"
    assert service.service_argv("py", 2.5)[-1] == "2.5"


def test_systemd_unit_has_execstart_home_and_restart(tmp_path):
    argv = service.service_argv("/venv/bin/python", 30)
    unit = service.render_systemd_unit(argv, tmp_path / ".jigga")
    assert f"ExecStart={' '.join(argv)}" in unit
    assert f"Environment=JIGGA_HOME={tmp_path / '.jigga'}" in unit
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit


def test_launchd_plist_has_label_args_keepalive_and_escapes(tmp_path):
    argv = service.service_argv("/venv/bin/py & co", 60)  # ampersand must be escaped
    plist = service.render_launchd_plist(argv, tmp_path, tmp_path / "logs")
    assert f"<string>{service.LAUNCHD_LABEL}</string>" in plist
    assert "<string>-m</string>" in plist and "<string>jigga</string>" in plist
    assert "<key>KeepAlive</key>" in plist and "<true/>" in plist
    assert "<key>JIGGA_HOME</key>" in plist
    assert "&amp;" in plist and "/venv/bin/py & co" not in plist  # raw ampersand gone


# ---- install ----------------------------------------------------------------

def test_install_systemd_writes_unit_and_starts(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    unit_path = tmp_path / "systemd" / service.SYSTEMD_UNIT
    monkeypatch.setattr(service, "detect_backend", lambda: "systemd")
    monkeypatch.setattr(service, "systemd_unit_path", lambda: unit_path)
    run_fn = _recorder()

    result = service.install_service(paths, interval_seconds=45, python="/venv/bin/python", run_fn=run_fn)

    assert result["backend"] == "systemd"
    assert result["started"] is True
    assert unit_path.exists()
    assert "ExecStart=/venv/bin/python -m jigga supervisor start --interval-seconds 45" \
        in unit_path.read_text()
    # daemon-reload then enable --now
    assert run_fn.calls[0] == ["systemctl", "--user", "daemon-reload"]
    assert run_fn.calls[1] == ["systemctl", "--user", "enable", "--now", service.SYSTEMD_UNIT]


def test_install_dry_run_writes_nothing(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    unit_path = tmp_path / "systemd" / service.SYSTEMD_UNIT
    monkeypatch.setattr(service, "detect_backend", lambda: "systemd")
    monkeypatch.setattr(service, "systemd_unit_path", lambda: unit_path)
    run_fn = _recorder()

    result = service.install_service(paths, dry_run=True, run_fn=run_fn)

    assert result["dry_run"] is True
    assert not unit_path.exists()
    assert run_fn.calls == []  # nothing executed
    assert all(c["ran"] is False for c in result["commands"])
    assert "unit_content" in result


def test_install_launchd_tolerates_failing_bootout(tmp_path, monkeypatch):
    """The leading `launchctl bootout` legitimately fails when the agent isn't
    loaded yet — that must NOT mark the install as failed."""
    paths = get_paths(tmp_path / "home")
    plist = tmp_path / "LaunchAgents" / f"{service.LAUNCHD_LABEL}.plist"
    monkeypatch.setattr(service, "detect_backend", lambda: "launchd")
    monkeypatch.setattr(service, "launchd_plist_path", lambda: plist)
    run_fn = _recorder(codes={0: 1})  # bootout fails, bootstrap/enable/kickstart succeed

    result = service.install_service(paths, run_fn=run_fn)

    assert result["started"] is True
    assert plist.exists()


def test_install_launchd_fails_when_bootstrap_fails(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    plist = tmp_path / "LaunchAgents" / f"{service.LAUNCHD_LABEL}.plist"
    monkeypatch.setattr(service, "detect_backend", lambda: "launchd")
    monkeypatch.setattr(service, "launchd_plist_path", lambda: plist)
    run_fn = _recorder(codes={1: 1})  # bootstrap (the real load) fails

    result = service.install_service(paths, run_fn=run_fn)

    assert result["started"] is False


def test_install_unsupported_gives_instructions(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    monkeypatch.setattr(service, "detect_backend", lambda: "unsupported")

    result = service.install_service(paths, python="/venv/bin/python")

    assert result["backend"] == "unsupported"
    assert "supervisor start" in result["instructions"]
    assert "unit_path" not in result


# ---- uninstall / status -----------------------------------------------------

def test_uninstall_removes_unit(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    unit_path = tmp_path / "systemd" / service.SYSTEMD_UNIT
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text("stub")
    monkeypatch.setattr(service, "detect_backend", lambda: "systemd")
    monkeypatch.setattr(service, "systemd_unit_path", lambda: unit_path)
    run_fn = _recorder()

    result = service.uninstall_service(paths, run_fn=run_fn)

    assert result["removed"] is True
    assert not unit_path.exists()
    assert ["systemctl", "--user", "disable", "--now", service.SYSTEMD_UNIT] in run_fn.calls


def test_status_reflects_unit_presence_and_run_state(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    unit_path = tmp_path / "systemd" / service.SYSTEMD_UNIT
    monkeypatch.setattr(service, "detect_backend", lambda: "systemd")
    monkeypatch.setattr(service, "systemd_unit_path", lambda: unit_path)

    # not installed, inactive
    inactive = service.status_service(paths, run_fn=lambda a: _proc(a, stdout="inactive\n"))
    assert inactive["installed"] is False
    assert inactive["running"] is False

    # installed + active
    unit_path.parent.mkdir(parents=True)
    unit_path.write_text("stub")
    active = service.status_service(paths, run_fn=lambda a: _proc(a, stdout="active\n"))
    assert active["installed"] is True
    assert active["running"] is True

"""Tests for the supervisor autostart service layer (jigga/runtime/service.py).

The OS-touching parts (launchctl/systemctl, the real LaunchAgents/systemd dirs)
are isolated by monkeypatching the backend detector + unit-path helpers and
injecting a fake ``run_fn``, so these run identically on any CI host.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

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
    monkeypatch.setattr(service, "systemd_unit_path", lambda system=False: unit_path)
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
    monkeypatch.setattr(service, "systemd_unit_path", lambda system=False: unit_path)
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
    monkeypatch.setattr(service, "launchd_plist_path", lambda system=False: plist)
    run_fn = _recorder(codes={0: 1})  # bootout fails, bootstrap/enable/kickstart succeed

    result = service.install_service(paths, run_fn=run_fn)

    assert result["started"] is True
    assert plist.exists()


def test_install_launchd_fails_when_bootstrap_fails(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    plist = tmp_path / "LaunchAgents" / f"{service.LAUNCHD_LABEL}.plist"
    monkeypatch.setattr(service, "detect_backend", lambda: "launchd")
    monkeypatch.setattr(service, "launchd_plist_path", lambda system=False: plist)
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
    monkeypatch.setattr(service, "systemd_unit_path", lambda system=False: unit_path)
    run_fn = _recorder()

    result = service.uninstall_service(paths, run_fn=run_fn)

    assert result["removed"] is True
    assert not unit_path.exists()
    assert ["systemctl", "--user", "disable", "--now", service.SYSTEMD_UNIT] in run_fn.calls


def test_status_reflects_unit_presence_and_run_state(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    unit_path = tmp_path / "systemd" / service.SYSTEMD_UNIT
    monkeypatch.setattr(service, "detect_backend", lambda: "systemd")
    monkeypatch.setattr(service, "systemd_unit_path", lambda system=False: unit_path)

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


def test_systemd_install_explicitly_restarts(tmp_path) -> None:
    """`enable --now` is a no-op on an already-active unit — without an
    explicit restart, a re-install (jigga update's daemon refresh, plugin
    re-start) keeps running OLD code/config on systemd."""
    from unittest.mock import patch

    import subprocess as sp

    from jigga.core.paths import get_paths
    from jigga.runtime.service import install_app_service, install_service

    paths = get_paths(tmp_path)
    calls: list[list[str]] = []

    def record(cmd):
        calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("jigga.runtime.service.detect_backend", return_value="systemd"), \
         patch("pathlib.Path.home", return_value=tmp_path):
        install_service(paths, run_fn=record)
        assert ["systemctl", "--user", "restart", "jigga-supervisor.service"] in calls

        calls.clear()
        install_app_service("viewer", ["node", "s.js"], cwd=tmp_path, env={},
                            logs_dir=paths.logs, run_fn=record)
        assert ["systemctl", "--user", "restart", "jigga-plugin-viewer.service"] in calls


# ---- stop / start -----------------------------------------------------------

def test_stop_systemd(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    monkeypatch.setattr(service, "detect_backend", lambda: "systemd")
    run_fn = _recorder()
    result = service.stop_service(paths, run_fn=run_fn)
    assert result["stopped"] is True
    assert run_fn.calls == [["systemctl", "--user", "stop", service.SYSTEMD_UNIT]]


def test_start_systemd(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    monkeypatch.setattr(service, "detect_backend", lambda: "systemd")
    run_fn = _recorder()
    result = service.start_service(paths, run_fn=run_fn)
    assert result["started"] is True
    assert run_fn.calls == [["systemctl", "--user", "start", service.SYSTEMD_UNIT]]


def test_start_launchd_bootstrap_optional_then_kickstart(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    monkeypatch.setattr(service, "detect_backend", lambda: "launchd")
    monkeypatch.setattr(service, "launchd_plist_path", lambda system=False: tmp_path / "x.plist")
    # bootstrap fails (already loaded) but that's optional → still started
    run_fn = _recorder(codes={0: 1})
    result = service.start_service(paths, run_fn=run_fn)
    assert result["started"] is True
    assert run_fn.calls[0][:2] == ["launchctl", "bootstrap"]
    assert run_fn.calls[1][:3] == ["launchctl", "kickstart", "-k"]


def test_stop_unsupported_backend(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    monkeypatch.setattr(service, "detect_backend", lambda: "unsupported")
    assert service.stop_service(paths, run_fn=_recorder())["stopped"] is False
    assert service.start_service(paths, run_fn=_recorder())["started"] is False


def test_cli_service_stop_start(tmp_path, monkeypatch):
    from jigga.cli import main
    monkeypatch.setattr(service, "detect_backend", lambda: "systemd")
    monkeypatch.setattr(service, "_default_run", lambda argv: _proc(argv, 0))
    assert main(["--home", str(tmp_path), "service", "stop"]) == 0
    assert main(["--home", str(tmp_path), "service", "start"]) == 0


def test_the_runner_is_resolved_at_call_time(tmp_path, monkeypatch):
    """Patching `_default_run` must actually replace the runner.

    It did not: `run_fn: RunFn = _default_run` bound the module function into
    the signature at import time, so every caller that omitted `run_fn` — the
    whole CLI — reached the ORIGINAL no matter what a test patched. The test
    above looked isolated and was really running `systemctl --user stop
    jigga-supervisor.service` against the developer's own machine, passing
    because the live service genuinely stopped.
    """
    paths = get_paths(tmp_path / "home")
    monkeypatch.setattr(service, "detect_backend", lambda: "systemd")
    seen = []

    def fake(argv):
        seen.append(argv)
        return _proc(argv, 0)

    monkeypatch.setattr(service, "_default_run", fake)
    service.stop_service(paths)      # no run_fn — the CLI's exact call shape
    assert seen == [["systemctl", "--user", "stop", service.SYSTEMD_UNIT]]


# ---- --system (system-level units) -----------------------------------------

def test_systemd_system_unit_runs_as_user_and_multi_user_target(tmp_path):
    argv = service.service_argv("/venv/bin/python", 60)
    unit = service.render_systemd_unit(argv, tmp_path / ".jigga", run_as="alice")
    assert "User=alice" in unit
    assert "WantedBy=multi-user.target" in unit  # boots before login (vs default.target)


def test_launchd_daemon_has_username(tmp_path):
    argv = service.service_argv("py", 60)
    plist = service.render_launchd_plist(argv, tmp_path, tmp_path / "logs", run_as="bob")
    assert "<key>UserName</key>" in plist and "<string>bob</string>" in plist


def test_system_paths():
    assert service.systemd_unit_path(system=True) == Path("/etc/systemd/system") / service.SYSTEMD_UNIT
    assert service.launchd_plist_path(system=True) == Path("/Library/LaunchDaemons") / f"{service.LAUNCHD_LABEL}.plist"


def test_install_system_uses_system_bus_and_path(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    unit_path = tmp_path / "etc" / service.SYSTEMD_UNIT
    monkeypatch.setattr(service, "detect_backend", lambda: "systemd")
    monkeypatch.setattr(service, "systemd_unit_path", lambda system=False: unit_path)
    run_fn = _recorder()
    result = service.install_service(paths, system=True, python="/venv/bin/python", run_fn=run_fn)
    assert result["system"] is True and result["started"] is True
    # system bus → no `--user`
    assert run_fn.calls[0] == ["systemctl", "daemon-reload"]
    assert "User=" in unit_path.read_text()


def test_stop_start_system_use_system_bus(tmp_path, monkeypatch):
    paths = get_paths(tmp_path / "home")
    monkeypatch.setattr(service, "detect_backend", lambda: "systemd")
    stop = service.stop_service(paths, system=True, run_fn=_recorder())
    start = service.start_service(paths, system=True, run_fn=_recorder())
    assert stop["commands"][0]["argv"] == ["systemctl", "stop", service.SYSTEMD_UNIT]
    assert start["commands"][0]["argv"] == ["systemctl", "start", service.SYSTEMD_UNIT]


def test_user_install_unchanged_no_user_directive(tmp_path, monkeypatch):
    # regression: a plain --user install still targets the user bus + no User=.
    paths = get_paths(tmp_path / "home")
    unit_path = tmp_path / "systemd" / service.SYSTEMD_UNIT
    monkeypatch.setattr(service, "detect_backend", lambda: "systemd")
    monkeypatch.setattr(service, "systemd_unit_path", lambda system=False: unit_path)
    result = service.install_service(paths, run_fn=_recorder())
    assert result["system"] is False
    assert "User=" not in unit_path.read_text() and "WantedBy=default.target" in unit_path.read_text()

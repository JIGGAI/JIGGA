"""Tests for `jigga doctor` (jigga/runtime/doctor.py + cli._cmd_doctor).

The service check reads the real ~/.config/systemd path, and the model/channel
checks read the runtime — so the system-touching ones are monkeypatched to keep
results deterministic on any host.
"""

from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.paths import get_paths
from jigga.runtime import doctor


def _unsupported_service(monkeypatch):
    monkeypatch.setattr("jigga.runtime.service.status_service",
                        lambda paths, **k: {"backend": "unsupported"})


def test_python_check_ok_on_supported_runtime():
    c = doctor._check_python()
    assert c.status == doctor.OK  # the suite runs on 3.11+


def test_uninitialized_runtime_fails(tmp_path: Path, monkeypatch):
    _unsupported_service(monkeypatch)
    report = doctor.run_checks(get_paths(tmp_path / "nope"))
    runtime = next(c for c in report.checks if c.name == "runtime")
    assert runtime.status == doctor.FAIL
    assert report.failed is True
    # runtime-dependent checks are skipped when home is absent
    assert not any(c.name == "model" for c in report.checks)


def test_initialized_runtime_has_no_failures(tmp_path: Path, monkeypatch):
    _unsupported_service(monkeypatch)
    init_runtime(tmp_path)
    report = doctor.run_checks(get_paths(tmp_path))
    assert report.failed is False
    names = {c.name for c in report.checks}
    assert {"python", "runtime", "config", "default_agent", "model", "channels", "service"} <= names


def test_config_errors_make_doctor_fail(tmp_path: Path, monkeypatch):
    _unsupported_service(monkeypatch)
    init_runtime(tmp_path)
    monkeypatch.setattr("jigga.runtime.validation.validate_configs",
                        lambda agents, teams: ["agent bad: wake.schedules[0] cron must have 5 fields"])
    report = doctor.run_checks(get_paths(tmp_path))
    config = next(c for c in report.checks if c.name == "config")
    assert config.status == doctor.FAIL
    assert report.failed is True


def test_config_warnings_do_not_fail(tmp_path: Path, monkeypatch):
    _unsupported_service(monkeypatch)
    init_runtime(tmp_path)
    monkeypatch.setattr("jigga.runtime.validation.validate_configs",
                        lambda agents, teams: ["warning: team t: handoff to 'x' is not a member"])
    report = doctor.run_checks(get_paths(tmp_path))
    config = next(c for c in report.checks if c.name == "config")
    assert config.status == doctor.WARN
    assert report.failed is False


def test_channels_check_warns_when_routed_agent_cant_reply(tmp_path: Path, monkeypatch):
    """A channel enabled but whose routed agent lacks the send tool => WARN
    (the user's exact silent-drop case)."""
    _unsupported_service(monkeypatch)
    init_runtime(tmp_path)
    from jigga.core.io import write_yaml
    write_yaml(tmp_path / "config.yaml",
               {"channels": {"telegram": {"enabled": True, "default_agent": "assistant"}}})
    # routed agent exists but has no telegram.send_message
    write_yaml(tmp_path / "agents" / "assistant.yaml",
               {"id": "assistant", "name": "A", "role": "pa", "default": True, "tools": ["filesystem.read"]})

    report = doctor.run_checks(get_paths(tmp_path))
    channels = next(c for c in report.checks if c.name == "channels")
    assert channels.status == doctor.WARN
    assert "won't send" in channels.detail

    # grant the tool -> OK
    write_yaml(tmp_path / "agents" / "assistant.yaml",
               {"id": "assistant", "name": "A", "role": "pa", "default": True,
                "tools": ["filesystem.read", "telegram.send_message"]})
    channels = next(c for c in doctor.run_checks(get_paths(tmp_path)).checks if c.name == "channels")
    assert channels.status == doctor.OK


def test_channels_check_warns_when_agent_cant_reach_network(tmp_path: Path, monkeypatch):
    """The user's exact case: agent has the send tool but no network egress to the
    channel host => WARN 'can't reach', not a false OK."""
    _unsupported_service(monkeypatch)
    from jigga.cli import _channels_setup

    init_runtime(tmp_path)
    paths = get_paths(tmp_path)
    from jigga.core.io import write_yaml
    write_yaml(paths.agents / "assistant.yaml",
               {"id": "assistant", "name": "A", "role": "pa", "default": True,
                "permission_mode": "autonomous", "tools": []})
    answers = iter(["1", "123456789:AAEdummytokendummytokendummytoken00", "n", "111", "assistant", "1"])
    _channels_setup(paths, prompt=lambda _p: next(answers), echo=lambda *_a, **_k: None)

    # full setup grants tool + network -> OK
    channels = next(c for c in doctor.run_checks(paths).checks if c.name == "channels")
    assert channels.status == doctor.OK

    # strip the network egress (simulate a pre-fix install) -> WARN can't reach
    from jigga.core.io import read_yaml
    doc = read_yaml(paths.agents / "assistant.yaml")
    doc["permissions"]["network"] = {"mode": "ask"}
    write_yaml(paths.agents / "assistant.yaml", doc)
    channels = next(c for c in doctor.run_checks(paths).checks if c.name == "channels")
    assert channels.status == doctor.WARN
    assert "can't reach" in channels.detail


def test_cli_exit_code_and_json(tmp_path: Path, monkeypatch, capsys):
    _unsupported_service(monkeypatch)
    # uninitialized -> non-zero
    assert main(["--home", str(tmp_path / "nope"), "doctor"]) == 1
    # initialized -> zero
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "doctor"]) == 0
    # --json is machine-readable
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert "summary" in payload and payload["summary"]["fail"] == 0


# --- doctor --fix ----------------------------------------------------------


def test_fix_repairs_broken_runtime(tmp_path: Path, monkeypatch):
    _unsupported_service(monkeypatch)
    home = tmp_path / "nope"
    report = doctor.run_checks(get_paths(home))
    assert report.failed
    actions = doctor.run_fixes(get_paths(home), report)
    assert any(a["check"] == "runtime" and a["fixed"] for a in actions)
    after = doctor.run_checks(get_paths(home))
    assert not after.failed and (home / "agents").exists()


def test_fix_installs_service_when_missing(tmp_path: Path, monkeypatch):
    calls: dict[str, bool] = {}
    monkeypatch.setattr("jigga.runtime.service.detect_backend", lambda: "systemd")
    monkeypatch.setattr("jigga.runtime.service.install_service",
                        lambda p, **k: (calls.__setitem__("install", True), {"backend": "systemd", "started": True})[1])
    monkeypatch.setattr("jigga.runtime.service.start_service",
                        lambda p, **k: (calls.__setitem__("start", True), {"backend": "systemd", "started": True})[1])
    report = doctor.Report(checks=[doctor.Check(
        "service", doctor.WARN, "Supervisor not installed as a service (won't survive reboot)")])
    actions = doctor.run_fixes(get_paths(tmp_path), report)
    assert calls.get("install") and not calls.get("start")
    assert actions[0]["fixed"] and "install" in actions[0]["message"].lower()


def test_fix_restarts_stopped_service(tmp_path: Path, monkeypatch):
    calls: dict[str, bool] = {}
    monkeypatch.setattr("jigga.runtime.service.detect_backend", lambda: "systemd")
    monkeypatch.setattr("jigga.runtime.service.install_service",
                        lambda p, **k: (calls.__setitem__("install", True), {"started": True})[1])
    monkeypatch.setattr("jigga.runtime.service.start_service",
                        lambda p, **k: (calls.__setitem__("start", True), {"backend": "systemd", "started": True})[1])
    report = doctor.Report(checks=[doctor.Check(
        "service", doctor.WARN, "Supervisor service installed but not running (systemd)")])
    actions = doctor.run_fixes(get_paths(tmp_path), report)
    assert calls.get("start") and not calls.get("install")
    assert "restart" in actions[0]["message"].lower()


def test_fix_skips_unfixable_checks(tmp_path: Path, monkeypatch):
    # config/model/etc have no auto-fix — run_fixes leaves them to the hint.
    report = doctor.Report(checks=[doctor.Check("model", doctor.WARN, "No model provider configured")])
    assert doctor.run_fixes(get_paths(tmp_path), report) == []


def test_cli_doctor_fix_json_includes_fixes(tmp_path: Path, monkeypatch, capsys):
    _unsupported_service(monkeypatch)
    home = tmp_path / "nope"
    rc = main(["--home", str(home), "doctor", "--fix", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert "fixes" in out and any(f["check"] == "runtime" and f["fixed"] for f in out["fixes"])
    assert out["ok"] is True and rc == 0  # runtime repaired → no failures

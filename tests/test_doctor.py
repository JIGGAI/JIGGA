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

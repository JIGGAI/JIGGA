from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from jigga.cli import main
from jigga.runtime.auth import (
    SUPPORTED_BACKENDS,
    BackendAuthStatus,
    auth_status,
    run_external_login,
)


# --- auth_status -----------------------------------------------------------


def test_auth_status_returns_an_entry_per_supported_backend() -> None:
    statuses = auth_status()
    assert {s.backend for s in statuses} == set(SUPPORTED_BACKENDS.keys())


def test_auth_status_reports_unavailable_when_binary_missing(monkeypatch) -> None:
    monkeypatch.setattr("jigga.runtime.auth.shutil.which", lambda _: None)
    statuses = auth_status()
    for status in statuses:
        assert status.binary_available is False
        assert status.binary_path is None


def test_auth_status_reports_available_when_binary_on_path(monkeypatch) -> None:
    monkeypatch.setattr(
        "jigga.runtime.auth.shutil.which",
        lambda binary: f"/usr/local/bin/{binary}",
    )
    statuses = auth_status()
    for status in statuses:
        assert status.binary_available is True
        assert status.binary_path == f"/usr/local/bin/{status.binary}"


def test_auth_status_to_dict_shape() -> None:
    status = BackendAuthStatus(
        backend="codex_cli",
        binary="codex",
        binary_available=True,
        binary_path="/usr/bin/codex",
        config_dir="~/.codex",
        install_url="https://example.com",
    )
    payload = status.to_dict()
    assert payload == {
        "backend": "codex_cli",
        "binary": "codex",
        "available": True,
        "path": "/usr/bin/codex",
        "config_dir": "~/.codex",
        "install_url": "https://example.com",
    }


# --- run_external_login ----------------------------------------------------


def test_run_external_login_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="Unknown auth backend"):
        run_external_login("openai_finetune")


def test_run_external_login_raises_when_binary_missing(monkeypatch) -> None:
    monkeypatch.setattr("jigga.runtime.auth.shutil.which", lambda _: None)
    with pytest.raises(FileNotFoundError, match="not installed or not on PATH"):
        run_external_login("codex_cli")


def test_run_external_login_invokes_upstream_cli(monkeypatch) -> None:
    monkeypatch.setattr("jigga.runtime.auth.shutil.which", lambda _: "/usr/bin/codex")
    fake_run = MagicMock(return_value=subprocess.CompletedProcess(["codex", "login"], 0))
    monkeypatch.setattr("jigga.runtime.auth.subprocess.run", fake_run)
    code = run_external_login("codex_cli")
    assert code == 0
    cmd_args = fake_run.call_args.args[0]
    assert cmd_args == ["codex", "login"]
    # Critical: must NOT pass an `env=` kwarg — login needs the full user
    # environment (browser, locale, TTY).
    assert "env" not in fake_run.call_args.kwargs


def test_run_external_login_returns_upstream_exit_code(monkeypatch) -> None:
    monkeypatch.setattr("jigga.runtime.auth.shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        "jigga.runtime.auth.subprocess.run",
        lambda *_, **__: subprocess.CompletedProcess(["claude", "login"], 7),
    )
    assert run_external_login("claude_code") == 7


# --- CLI integration -------------------------------------------------------


def test_cli_auth_status_outputs_json(capsys) -> None:
    assert main(["auth", "status"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert isinstance(output, list)
    assert all("backend" in entry for entry in output)


def test_cli_auth_login_propagates_exit_code(monkeypatch) -> None:
    monkeypatch.setattr("jigga.cli.run_external_login", lambda backend: 42)
    assert main(["auth", "login", "codex_cli"]) == 42


def test_cli_auth_login_surfaces_clean_error_for_missing_binary(monkeypatch, capsys) -> None:
    monkeypatch.setattr("jigga.runtime.auth.shutil.which", lambda _: None)
    # Top-level CLI converts the exception to stderr + exit 1
    assert main(["auth", "login", "codex_cli"]) == 1
    assert "not installed or not on PATH" in capsys.readouterr().err

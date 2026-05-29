from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from jigga.runtime.sandbox import (
    BASE_ENV_ALLOWLIST,
    SandboxSpec,
    build_restricted_env,
    run_sandboxed,
)


def test_base_env_allowlist_contains_expected_vars() -> None:
    # These are documented public defaults; changes here are behavior changes
    # for every subprocess spawner. Pin them so additions are explicit.
    assert BASE_ENV_ALLOWLIST == frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TERM"})


def test_build_restricted_env_excludes_unrequested_secrets(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/sandbox/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "also-not")
    env = build_restricted_env([])
    assert env["PATH"] == "/sandbox/bin"
    assert "OPENAI_API_KEY" not in env
    assert "AWS_ACCESS_KEY_ID" not in env


def test_build_restricted_env_includes_explicitly_requested_secrets(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "gh-pat-test")
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak")
    env = build_restricted_env(["GITHUB_TOKEN"])
    assert env["GITHUB_TOKEN"] == "gh-pat-test"
    assert "OPENAI_API_KEY" not in env


def test_build_restricted_env_silently_omits_unset_secrets(monkeypatch) -> None:
    monkeypatch.delenv("DOES_NOT_EXIST_KEY", raising=False)
    env = build_restricted_env(["DOES_NOT_EXIST_KEY"])
    assert "DOES_NOT_EXIST_KEY" not in env


def test_run_sandboxed_executes_in_specified_cwd(tmp_path: Path) -> None:
    spec = SandboxSpec(
        command=sys.executable,
        args=["-c", "import os; print(os.getcwd())"],
        cwd=tmp_path,
    )
    completed = run_sandboxed(spec)
    assert completed.returncode == 0
    assert completed.stdout.strip() == str(tmp_path.resolve())


def test_run_sandboxed_passes_only_allowlisted_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    spec = SandboxSpec(
        command=sys.executable,
        args=[
            "-c",
            "import os, json; "
            "print(json.dumps({"
            "'has_openai': 'OPENAI_API_KEY' in os.environ, "
            "'has_github': 'GITHUB_TOKEN' in os.environ"
            "}))",
        ],
        cwd=tmp_path,
        secrets_required=["GITHUB_TOKEN"],
    )
    completed = run_sandboxed(spec)
    assert completed.returncode == 0
    import json

    env_seen = json.loads(completed.stdout.strip())
    assert env_seen["has_openai"] is False
    assert env_seen["has_github"] is True


def test_run_sandboxed_honors_timeout(tmp_path: Path) -> None:
    spec = SandboxSpec(
        command=sys.executable,
        args=["-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        timeout_seconds=0.3,
    )
    with pytest.raises(subprocess.TimeoutExpired):
        run_sandboxed(spec)


def test_run_sandboxed_captures_stdout_and_stderr(tmp_path: Path) -> None:
    spec = SandboxSpec(
        command=sys.executable,
        args=["-c", "import sys; print('hi out'); print('hi err', file=sys.stderr)"],
        cwd=tmp_path,
    )
    completed = run_sandboxed(spec)
    assert "hi out" in completed.stdout
    assert "hi err" in completed.stderr


def test_run_sandboxed_returns_nonzero_without_raising(tmp_path: Path) -> None:
    spec = SandboxSpec(
        command=sys.executable,
        args=["-c", "raise SystemExit(7)"],
        cwd=tmp_path,
    )
    completed = run_sandboxed(spec)
    assert completed.returncode == 7  # check=False semantics

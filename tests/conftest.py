"""Pytest configuration for JIGGA tests.

Auto-enables notification dry-run mode for the whole test session so unit
tests never actually pop a desktop notification while exercising the
`notifications.send` capability against a real workflow run.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _disable_real_notifications_in_tests() -> None:
    # Tests can still exercise the real send_notification path explicitly by
    # passing dry_run=False; this fixture only changes the *default* the
    # dispatcher uses when reading the runtime config.
    os.environ["JIGGA_NOTIFICATION_MODE"] = "dry_run"
    yield
    os.environ.pop("JIGGA_NOTIFICATION_MODE", None)


@pytest.fixture(autouse=True)
def _isolate_from_real_system(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Tests never touch the real system (or see its state).

    Project auto-discovery walks UP from the test process's cwd looking for a
    `.jigga/` dir — from a repo checkout under $HOME it reaches the developer's
    real `~/.jigga`, so whatever capabilities/config live there bleed into
    tests that pass `--home tmp_path` and believe they're isolated (found
    live: a telegram capability installed on the dev box turned a green
    `capabilities pending` assertion red). Pin both discovery roots to
    per-test temp dirs; tests that exercise discovery itself pass explicit
    paths or set these envs themselves, which still wins."""
    monkeypatch.setenv("JIGGA_PROJECT", str(tmp_path))
    monkeypatch.setenv("JIGGA_HOME", str(tmp_path / ".jigga-test-home"))

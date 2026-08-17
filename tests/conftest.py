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


@pytest.fixture
def grant():
    """Grant capability actions to an existing agent yaml.

    Dispatch denies any action an agent wasn't explicitly granted — the grant
    list is the security boundary, not just the menu the model is offered — so
    a test driving a workflow step has to grant it exactly as a real install
    would. Deliberately not autouse: a test that forgets should fail.
    """
    from jigga.core.io import read_yaml, write_yaml

    def _grant(paths, agent_id: str, *actions: str) -> None:
        path = paths.agents / f"{agent_id}.yaml"
        doc = read_yaml(path)
        doc["tools"] = list(dict.fromkeys([*(doc.get("tools") or []), *actions]))
        write_yaml(path, doc)

    return _grant


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


@pytest.fixture(autouse=True)
def _no_real_service_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test ever drives the machine's real launchd/systemd.

    `service.py` takes an injectable `run_fn` for exactly this reason, and
    `test_cli_service_stop_start` monkeypatched `service._default_run` believing
    that covered it. It did not: the signatures read
    `run_fn: RunFn = _default_run`, which binds the module function into the
    signature at import time, so the CLI path called the ORIGINAL. On a dev box
    with a systemd user session, running the suite genuinely executed
    `systemctl --user stop jigga-supervisor.service` against the live
    supervisor, then started it again — and passed, because the real service
    really did stop. It only failed on CI, where there is no user session.

    The signatures now resolve `run_fn` at call time, so injection works. This
    fixture is the guardrail that keeps it working: reaching the real runner is
    a loud failure, not a silently successful side effect on someone's laptop.
    A test that means to exercise `_default_run` monkeypatches it explicitly,
    which still wins over an autouse fixture.
    """
    from jigga.runtime import service

    def _refuse(argv: list[str]):
        raise AssertionError(
            f"a test tried to run the real service manager: {' '.join(argv)}. "
            "Pass run_fn=<fake> (see tests/test_service.py::_recorder)."
        )

    monkeypatch.setattr(service, "_default_run", _refuse)

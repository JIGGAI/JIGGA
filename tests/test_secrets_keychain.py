"""E1b: keychain backend (fake secret-tool shell-outs), auto-degrade on
headless boxes, file fallthrough for pre-keychain secrets, and chatgpt_auth
routing through the broker (jigga store) while codex stays file-based."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime import secrets_broker as broker


@pytest.fixture(autouse=True)
def _reset_probe():
    broker._keychain_probe = None
    yield
    broker._keychain_probe = None


class _FakeKeychain:
    """In-memory stand-in for secret-tool (Linux shape)."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def run(self, cmd, capture_output=False, text=False, check=False, input=None):
        verb = cmd[1] if cmd[0] == "secret-tool" else None
        name = cmd[-1]
        if verb == "lookup":
            value = self.store.get(name)
            return subprocess.CompletedProcess(cmd, 0 if value is not None else 1,
                                               stdout=value or "", stderr="")
        if verb == "store":
            self.store[name] = input
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if verb == "clear":
            return subprocess.CompletedProcess(cmd, 0 if self.store.pop(name, None) is not None else 1,
                                               stdout="", stderr="")
        raise AssertionError(cmd)


@pytest.fixture()
def keychain(monkeypatch):
    fake = _FakeKeychain()
    monkeypatch.setattr(broker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(broker.shutil, "which", lambda name: "/usr/bin/secret-tool")
    monkeypatch.setattr(broker.subprocess, "run", fake.run)
    return fake


def test_auto_uses_keychain_when_probe_succeeds(tmp_path: Path, keychain) -> None:
    paths = init_runtime(tmp_path, examples=True)
    assert broker.resolved_backend(paths.home) == "keychain"
    location = broker.set_secret(paths.home, "tok", "v1")
    assert location == "keychain:jigga/tok"
    assert keychain.store["tok"] == "v1"
    assert broker.get_secret(paths.home, "tok") == "v1"
    assert not (paths.home / "secrets" / "tok").exists()  # nothing on disk
    assert broker.delete_secret(paths.home, "tok") is True
    assert broker.get_secret(paths.home, "tok") is None


def test_keychain_falls_through_to_preexisting_file(tmp_path: Path, keychain) -> None:
    paths = init_runtime(tmp_path, examples=True)
    (paths.home / "secrets").mkdir(exist_ok=True)
    (paths.home / "secrets" / "legacy").write_text("old", encoding="utf-8")
    assert broker.get_secret(paths.home, "legacy") == "old"  # pre-keychain secret still readable
    broker.set_secret(paths.home, "legacy", "new")           # set migrates it forward
    assert keychain.store["legacy"] == "new"
    assert broker.get_secret(paths.home, "legacy") == "new"  # keychain wins over the stale file


def test_auto_degrades_to_file_headless(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    monkeypatch.setattr(broker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(broker.shutil, "which", lambda name: "/usr/bin/secret-tool")
    monkeypatch.setattr(broker.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 2, stdout="", stderr="no session bus"))
    assert broker.resolved_backend(paths.home) == "file"  # probe exit 2 = no Secret Service


def test_explicit_file_backend_never_probes(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.config, {"secrets": {"backend": "file"}})

    def _boom(*_a, **_k):
        raise AssertionError("probe must not run for explicit file backend")

    monkeypatch.setattr(broker.subprocess, "run", _boom)
    assert broker.resolved_backend(paths.home) == "file"
    broker.set_secret(paths.home, "x", "y")
    assert broker.get_secret(paths.home, "x") == "y"


def test_chatgpt_auth_roundtrips_through_broker(tmp_path: Path) -> None:
    from jigga.runtime.chatgpt_auth import STORE_FILENAME, login_state, save_credentials

    paths = init_runtime(tmp_path, examples=True)
    save_credentials(paths.home, {"access_token": "", "refresh_token": "r"})
    # File backend → same legacy path, via the broker.
    stored = json.loads((paths.home / "secrets" / STORE_FILENAME).read_text(encoding="utf-8"))
    assert stored["tokens"]["refresh_token"] == "r"
    state = login_state(paths.home)
    assert state["source"] == "jigga" and state["logged_in"] is False  # empty access token


def test_chatgpt_force_refresh_persists_via_broker(tmp_path: Path, monkeypatch) -> None:
    from jigga.runtime import chatgpt_auth as auth

    paths = init_runtime(tmp_path, examples=True)
    save = auth.save_credentials(paths.home, {"access_token": "a1", "refresh_token": "r1"})
    assert save == auth.jigga_store(paths.home)
    monkeypatch.setattr(auth, "_refresh", lambda rt: {"access_token": "a2", "refresh_token": "r2"})
    creds = auth.load_credentials(home=paths.home)
    creds.force_refresh()
    stored = json.loads((paths.home / "secrets" / auth.STORE_FILENAME).read_text(encoding="utf-8"))
    assert stored["tokens"]["access_token"] == "a2"
    assert stored["tokens"]["refresh_token"] == "r2"

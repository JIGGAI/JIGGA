"""E1d: encrypted-file backend — real openssl roundtrip (self-contained tmp
files), plaintext-fallthrough migration, missing-passphrase contract, list/
delete handling of the .enc twin, and the migrate command."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.secrets_broker import delete_secret, get_secret, list_secrets, migrate_to_encrypted, set_secret

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")


@pytest.fixture()
def enc_home(tmp_path: Path, monkeypatch):
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.config, {"secrets": {"backend": "encrypted-file"}})
    monkeypatch.setenv("JIGGA_SECRETS_PASSPHRASE", "test-pass-1")
    return paths


def test_roundtrip_is_ciphertext_at_rest(enc_home) -> None:
    location = set_secret(enc_home.home, "tok", "sv-plain-value")
    assert location.endswith("tok.enc")
    on_disk = Path(location).read_bytes()
    assert b"sv-plain-value" not in on_disk  # actually encrypted
    assert get_secret(enc_home.home, "tok") == "sv-plain-value"
    assert list_secrets(enc_home.home) == ["tok"]  # suffix stripped
    assert delete_secret(enc_home.home, "tok") is True
    assert get_secret(enc_home.home, "tok") is None


def test_wrong_passphrase_fails_loudly(enc_home, monkeypatch) -> None:
    set_secret(enc_home.home, "tok", "v")
    monkeypatch.setenv("JIGGA_SECRETS_PASSPHRASE", "wrong")
    with pytest.raises(RuntimeError, match="passphrase"):
        get_secret(enc_home.home, "tok")


def test_missing_passphrase_is_a_clear_error(enc_home, monkeypatch) -> None:
    monkeypatch.delenv("JIGGA_SECRETS_PASSPHRASE")
    with pytest.raises(ValueError, match="JIGGA_SECRETS_PASSPHRASE"):
        set_secret(enc_home.home, "tok", "v")


def test_plaintext_fallthrough_then_set_encrypts(enc_home) -> None:
    (enc_home.home / "secrets").mkdir(exist_ok=True)
    (enc_home.home / "secrets" / "legacy").write_text("old", encoding="utf-8")
    assert get_secret(enc_home.home, "legacy") == "old"  # readable pre-migration
    set_secret(enc_home.home, "legacy", "new")
    assert not (enc_home.home / "secrets" / "legacy").exists()  # plaintext twin removed
    assert get_secret(enc_home.home, "legacy") == "new"


def test_migrate_encrypts_everything_and_sets_backend(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    monkeypatch.setenv("JIGGA_SECRETS_PASSPHRASE", "p1")
    set_secret(paths.home, "a_token", "va")  # file backend (default on headless)
    set_secret(paths.home, "b_token", "vb")
    migrated = migrate_to_encrypted(paths.home)
    assert migrated == ["a_token", "b_token"]
    assert read_yaml(paths.config)["secrets"]["backend"] == "encrypted-file"
    assert not (paths.home / "secrets" / "a_token").exists()
    assert get_secret(paths.home, "a_token") == "va"
    assert migrate_to_encrypted(paths.home) == []  # idempotent


def test_subagent_spec_declares_cli_home_binds(tmp_path: Path) -> None:
    from jigga.runtime.sandbox import SandboxSpec, bwrap_argv

    spec = SandboxSpec(command="codex", cwd=tmp_path,
                       fs_write=[Path.home() / ".definitely-not-real-dir-xyz", tmp_path / "real"])
    (tmp_path / "real").mkdir()
    argv = bwrap_argv(spec, {})
    joined = " ".join(argv)
    assert str(tmp_path / "real") in joined
    assert ".definitely-not-real-dir-xyz" not in joined  # missing sources filtered

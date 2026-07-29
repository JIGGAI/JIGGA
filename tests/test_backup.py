"""jigga backup create/inspect/restore: secrets excluded by default, manifest,
guarded restore with move-aside, hostile-archive safety, encryption contract."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.runtime.backup import create_backup, inspect_backup, restore_backup


def _seed(tmp_path: Path):
    paths = init_runtime(tmp_path, examples=True)
    (paths.home / "secrets").mkdir(exist_ok=True)
    (paths.home / "secrets" / "token").write_text("SECRET", encoding="utf-8")
    (paths.home / "runs").mkdir(exist_ok=True)
    (paths.home / "runs" / "junk.json").write_text("{}", encoding="utf-8")
    return paths


def _names(archive: Path) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getnames()


def test_create_excludes_secrets_and_ephemera_by_default(tmp_path: Path) -> None:
    paths = _seed(tmp_path)
    out = tmp_path / "b.tar.gz"
    result = create_backup(paths.home, out)
    names = _names(out)
    assert result["include_secrets"] is False and result["files"] > 0
    assert not any(n.startswith("secrets/") for n in names)
    assert not any(n.startswith("runs/") for n in names)
    assert "config.yaml" in names and any(n.startswith("agents/") for n in names)
    assert inspect_backup(out)["files"] == result["files"]


def test_include_secrets_is_explicit(tmp_path: Path) -> None:
    paths = _seed(tmp_path)
    out = tmp_path / "s.tar.gz"
    create_backup(paths.home, out, include_secrets=True)
    assert "secrets/token" in _names(out)


def test_create_refuses_overwrite(tmp_path: Path) -> None:
    paths = _seed(tmp_path)
    out = tmp_path / "b.tar.gz"
    out.write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        create_backup(paths.home, out)


def test_restore_roundtrip_into_empty_home(tmp_path: Path) -> None:
    paths = _seed(tmp_path)
    out = tmp_path / "b.tar.gz"
    create_backup(paths.home, out)
    target = tmp_path / "new-home"
    result = restore_backup(out, target)
    assert (target / "config.yaml").exists()
    assert not (target / "secrets").exists()
    assert not (target / "jigga-backup.json").exists()  # manifest not left behind
    assert result["moved_aside"] is None


def test_restore_nonempty_requires_force_and_moves_aside(tmp_path: Path) -> None:
    paths = _seed(tmp_path)
    out = tmp_path / "b.tar.gz"
    create_backup(paths.home, out)
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "precious.txt").write_text("keep me", encoding="utf-8")
    with pytest.raises(ValueError, match="--force"):
        restore_backup(out, target)
    result = restore_backup(out, target, force=True)
    moved = Path(result["moved_aside"])
    assert (moved / "precious.txt").read_text(encoding="utf-8") == "keep me"
    assert (target / "config.yaml").exists()


def test_restore_rejects_non_backup_and_encrypted_archives(tmp_path: Path) -> None:
    paths = _seed(tmp_path)
    plain = tmp_path / "not-jigga.tar.gz"
    with tarfile.open(plain, "w:gz") as tar:
        tar.add(paths.config, arcname="config.yaml")  # no manifest
    with pytest.raises(KeyError):
        restore_backup(plain, tmp_path / "t1")
    enc = tmp_path / "b.tar.gz.age"
    enc.write_bytes(b"age...")
    with pytest.raises(ValueError, match="Decrypt first"):
        restore_backup(enc, tmp_path / "t2")


def test_encrypt_requires_tool_and_removes_plaintext_on_refusal(tmp_path: Path, monkeypatch) -> None:
    import jigga.runtime.backup as mod

    paths = _seed(tmp_path)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    out = tmp_path / "e.tar.gz"
    with pytest.raises(ValueError, match="requires the `age` binary"):
        create_backup(paths.home, out, encrypt="age")
    assert not out.exists()  # no plaintext left behind when the contract fails


def test_cli_roundtrip(tmp_path: Path, capsys) -> None:
    import json

    _seed(tmp_path)
    out = tmp_path / "cli.tar.gz"
    assert main(["--home", str(tmp_path), "backup", "create", "--output", str(out)]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["archive"] == str(out)
    assert main(["--home", str(tmp_path), "backup", "inspect", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["files"] == created["files"]
    target = tmp_path / "restored"
    assert main(["--home", str(target), "backup", "restore", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["home"] == str(target)
    assert (target / "config.yaml").exists()

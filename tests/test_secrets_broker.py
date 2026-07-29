"""Secrets broker (E1a): file/env backends, name validation, CLI, and the
migrated readers (telegram / email / brave) round-tripping through it."""

from __future__ import annotations

from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.secrets_broker import delete_secret, get_secret, list_secrets, set_secret


def test_file_backend_roundtrip_0600(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    location = set_secret(paths.home, "api_key", "s3cret")
    assert get_secret(paths.home, "api_key") == "s3cret"
    assert (Path(location).stat().st_mode & 0o777) == 0o600
    assert list_secrets(paths.home) == ["api_key"]
    assert delete_secret(paths.home, "api_key") is True
    assert get_secret(paths.home, "api_key") is None


def test_name_validation_refuses_traversal(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    for bad in ("../etc/passwd", "a/b", "", ".hidden"):
        with pytest.raises(ValueError, match="Invalid secret name"):
            get_secret(paths.home, bad)


def test_env_backend_read_only(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.config, {"secrets": {"backend": "env"}})
    monkeypatch.setenv("JIGGA_SECRET_BRAVE_API_KEY", "from-env")
    assert get_secret(paths.home, "brave_api_key") == "from-env"
    assert "brave_api_key" in list_secrets(paths.home)
    with pytest.raises(ValueError, match="read-only"):
        set_secret(paths.home, "x", "y")


def test_existing_reader_files_unchanged(tmp_path: Path) -> None:
    """Broker file backend maps to the EXACT legacy filenames — zero migration."""
    from jigga.runtime.email_imap import load_credentials, store_credentials
    from jigga.runtime.telegram import load_bot_token, store_bot_token

    paths = init_runtime(tmp_path, examples=True)
    store_bot_token(paths.secrets, "123:AAE")
    assert (paths.secrets / "telegram_bot_token").read_text(encoding="utf-8") == "123:AAE"
    assert load_bot_token(paths.secrets) == "123:AAE"
    store_credentials(paths.home, {"imap_host": "i", "smtp_host": "s",
                                   "username": "u", "password": "p"})
    assert (paths.secrets / "email_imap.json").exists()
    assert load_credentials(paths.home)["imap_host"] == "i"


def test_brave_reader_uses_broker(tmp_path: Path, monkeypatch) -> None:
    from jigga.runtime import web

    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.config, {"web": {"search_provider": "brave"}})
    write_yaml(paths.config, {"web": {"search_provider": "brave"},
                              "secrets": {"backend": "env"}})
    monkeypatch.setenv("JIGGA_SECRET_BRAVE_API_KEY", "k-env")
    seen = {}

    class _Resp:
        status = 200

        def read(self, _n):
            return b'{"web": {"results": []}}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(web.urllib.request, "urlopen",
                        lambda req, timeout=0: seen.update(t=req.headers.get("X-subscription-token")) or _Resp())
    web.search(paths.home, "q")
    assert seen["t"] == "k-env"  # key came from the broker's env backend


def test_cli_list_and_delete(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path, examples=True)
    set_secret(paths.home, "tok", "v")
    assert main(["--home", str(tmp_path), "secrets", "list"]) == 0
    out = capsys.readouterr().out
    assert "tok" in out and "v" not in out
    assert main(["--home", str(tmp_path), "secrets", "delete", "tok"]) == 0
    assert "Deleted" in capsys.readouterr().out

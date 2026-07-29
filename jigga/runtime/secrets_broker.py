"""Secrets broker — Milestone E slice E1a (`docs/MILESTONE_E_DESIGN.md`).

Every secret read in the runtime goes through this one chokepoint. Named
secrets, pluggable backends:

- **file** (default): `~/.jigga/secrets/<name>`, 0600 — exactly the files the
  runtime has always used (`telegram_bot_token`, `email_imap.json`,
  `chatgpt_auth.json`, `brave_api_key`), so migrating readers onto the broker
  changes zero bytes on disk.
- **env**: explicit opt-in (`secrets.backend: env`) reading
  `JIGGA_SECRET_<NAME>` (name upper-cased, non-alnum → `_`) — CI/dev only.
- `auto` resolves to `file` today; the keychain backend (E1b) and
  encrypted-file backend (E1d) slot in here.

Values are opaque strings (JSON-valued credentials store the JSON text).
Names never contain path separators — the broker refuses traversal outright.
`list_secrets` returns names only; no API enumerates values.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from jigga.core.config import load_runtime_config
from jigga.core.io import ensure_dir

_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _backend(home: Path) -> str:
    secrets = load_runtime_config(home).get("secrets") or {}
    backend = str(secrets.get("backend") or "auto")
    return "file" if backend == "auto" else backend


def _validate(name: str) -> str:
    if not name or not _NAME.match(name) or name.startswith("."):
        raise ValueError(f"Invalid secret name: {name!r} (letters, digits, . _ - only)")
    return name


def _file_path(home: Path, name: str) -> Path:
    return Path(home) / "secrets" / name


def _env_var(name: str) -> str:
    return "JIGGA_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def get_secret(home: Path, name: str) -> str | None:
    """The secret's value, or None when unset. All runtime secret reads route
    here — never open files under secrets/ directly."""
    _validate(name)
    if _backend(home) == "env":
        return os.environ.get(_env_var(name))
    path = _file_path(home, name)
    return path.read_text(encoding="utf-8") if path.exists() else None


def set_secret(home: Path, name: str, value: str) -> str:
    """Store a secret; returns a display location (never the value)."""
    _validate(name)
    if _backend(home) == "env":
        raise ValueError(f"secrets.backend=env is read-only — export {_env_var(name)} instead.")
    path = _file_path(home, name)
    ensure_dir(path.parent)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def delete_secret(home: Path, name: str) -> bool:
    _validate(name)
    if _backend(home) == "env":
        raise ValueError("secrets.backend=env is read-only — unset the variable instead.")
    path = _file_path(home, name)
    if path.exists():
        path.unlink()
        return True
    return False


def list_secrets(home: Path) -> list[str]:
    """Names only — values never enumerate."""
    if _backend(home) == "env":
        return sorted(k[len("JIGGA_SECRET_"):].lower() for k in os.environ if k.startswith("JIGGA_SECRET_"))
    secrets_dir = Path(home) / "secrets"
    return sorted(p.name for p in secrets_dir.iterdir()
                  if p.is_file() and not p.name.startswith(".")) if secrets_dir.exists() else []

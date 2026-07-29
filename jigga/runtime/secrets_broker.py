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
import platform
import re
import shutil
import subprocess
from pathlib import Path

from jigga.core.config import load_runtime_config
from jigga.core.io import ensure_dir

_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
_KEYCHAIN_SERVICE = "jigga"
# Cached per process: probing the keychain (DBus roundtrip) once is plenty.
_keychain_probe: bool | None = None


def keychain_available() -> bool:
    """True when an OS keychain is actually usable — the CLI exists AND a probe
    succeeds. Headless servers (no DBus session) fail the probe and `auto`
    degrades silently to file, per the Milestone E design."""
    global _keychain_probe
    if _keychain_probe is not None:
        return _keychain_probe
    if platform.system() == "Darwin":
        _keychain_probe = shutil.which("security") is not None
        return _keychain_probe
    if shutil.which("secret-tool") is None:
        _keychain_probe = False
        return False
    probe = subprocess.run(["secret-tool", "lookup", "service", _KEYCHAIN_SERVICE,
                            "name", "__probe__"], capture_output=True, text=True, check=False)
    # Exit 1 = "not found" (bus reachable, keychain works); other codes = no
    # Secret Service (headless) → unusable.
    _keychain_probe = probe.returncode in (0, 1)
    return _keychain_probe


def _backend(home: Path) -> str:
    secrets = load_runtime_config(home).get("secrets") or {}
    backend = str(secrets.get("backend") or "auto")
    if backend == "auto":
        return "keychain" if keychain_available() else "file"
    return backend


def resolved_backend(home: Path) -> str:
    """The backend in effect (auto resolved) — surfaced by `jigga doctor`."""
    return _backend(home)


def _keychain_get(name: str) -> str | None:
    if platform.system() == "Darwin":
        cmd = ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", name, "-w"]
    else:
        cmd = ["secret-tool", "lookup", "service", _KEYCHAIN_SERVICE, "name", name]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.stdout.rstrip("\n") if result.returncode == 0 and result.stdout else None


def _keychain_set(name: str, value: str) -> str:
    if platform.system() == "Darwin":
        cmd = ["security", "add-generic-password", "-U", "-s", _KEYCHAIN_SERVICE,
               "-a", name, "-w", value]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    else:
        cmd = ["secret-tool", "store", f"--label=jigga:{name}",
               "service", _KEYCHAIN_SERVICE, "name", name]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, input=value)
    if result.returncode != 0:
        raise RuntimeError(f"keychain store failed for {name!r}: {result.stderr.strip()[:200]}")
    return f"keychain:{_KEYCHAIN_SERVICE}/{name}"


def _keychain_delete(name: str) -> bool:
    if platform.system() == "Darwin":
        cmd = ["security", "delete-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", name]
    else:
        cmd = ["secret-tool", "clear", "service", _KEYCHAIN_SERVICE, "name", name]
    return subprocess.run(cmd, capture_output=True, check=False).returncode == 0


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
    backend = _backend(home)
    if backend == "env":
        return os.environ.get(_env_var(name))
    if backend == "keychain":
        value = _keychain_get(name)
        if value is not None:
            return value
        # Fall through to file: pre-keychain secrets keep working, and
        # `jigga secrets set` on this backend migrates them forward.
    path = _file_path(home, name)
    return path.read_text(encoding="utf-8") if path.exists() else None


def set_secret(home: Path, name: str, value: str) -> str:
    """Store a secret; returns a display location (never the value)."""
    _validate(name)
    backend = _backend(home)
    if backend == "env":
        raise ValueError(f"secrets.backend=env is read-only — export {_env_var(name)} instead.")
    if backend == "keychain":
        return _keychain_set(name, value)
    path = _file_path(home, name)
    ensure_dir(path.parent)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def delete_secret(home: Path, name: str) -> bool:
    _validate(name)
    backend = _backend(home)
    if backend == "env":
        raise ValueError("secrets.backend=env is read-only — unset the variable instead.")
    deleted = _keychain_delete(name) if backend == "keychain" else False
    path = _file_path(home, name)
    if path.exists():
        path.unlink()
        return True
    return deleted


def list_secrets(home: Path) -> list[str]:
    """Names only — values never enumerate."""
    if _backend(home) == "env":
        return sorted(k[len("JIGGA_SECRET_"):].lower() for k in os.environ if k.startswith("JIGGA_SECRET_"))
    secrets_dir = Path(home) / "secrets"
    return sorted(p.name for p in secrets_dir.iterdir()
                  if p.is_file() and not p.name.startswith(".")) if secrets_dir.exists() else []

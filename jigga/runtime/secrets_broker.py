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

import contextlib
import os
import platform
import re
import shutil
import subprocess
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from jigga.core.config import load_runtime_config
from jigga.core.io import ensure_dir

# `@` separates a secret from the team it belongs to: `postiz_api_key@oakwood`.
# Two tenants needing different accounts for the same logical secret is a
# correctness constraint, not a convenience — see `resolve_secret_name`.
_NAME = re.compile(r"^[A-Za-z0-9._-]+(@[A-Za-z0-9._-]+)?$")
_TEAM_SEPARATOR = "@"

# E1c: when a capability invocation is in flight, the dispatcher binds
# (executing agent, logs_dir) here; get_secret enforces the agent's grant
# before releasing anything. Reads outside any capability context (login
# wizards, the supervisor's own channel polling, CLI) are runtime-trusted
# and unaffected.
_capability_ctx: ContextVar[tuple[Any, Path] | None] = ContextVar("jigga_secret_ctx", default=None)


@contextlib.contextmanager
def capability_secret_context(agent: Any, logs_dir: Path):
    token = _capability_ctx.set((agent, Path(logs_dir)))
    try:
        yield
    finally:
        _capability_ctx.reset(token)


def _enforce_grant(home: Path, name: str, agent: Any, logs_dir: Path) -> None:
    """Release policy for a secret read inside a capability invocation.

    - Agent declares a `permissions.secrets` block → it is evaluated; a name
      outside the grant is DENIED (audited).
    - No block declared → legacy-allow with an audited `granted: false`, so
      existing installs keep working while the audit log shows exactly which
      grants to add. `secrets.enforce_grants: true` flips missing-block to
      deny once an install has granted its agents.
    """
    from jigga.runtime.audit import append_event
    from jigga.runtime.policy import evaluate_resource_permission

    permissions = getattr(agent, "permissions", None) or {}
    has_block = isinstance(permissions, dict) and permissions.get("secrets") is not None
    strict = bool((load_runtime_config(home).get("secrets") or {}).get("enforce_grants"))
    if not has_block and not strict:
        append_event(logs_dir, "secret.released", agent=getattr(agent, "id", None),
                     name=name, granted=False)
        return
    decision = evaluate_resource_permission(agent, "secrets", name) if has_block else None
    if decision is None or decision.status != "allow":
        append_event(logs_dir, "secret.denied", status="denied",
                     agent=getattr(agent, "id", None), name=name,
                     reason=(decision.reason if decision else "no secrets grant (enforce_grants on)"))
        raise PermissionError(
            f"Secret {name!r} is not granted to agent "
            f"{getattr(agent, 'id', '?')!r} (permissions.secrets).")
    append_event(logs_dir, "secret.released", agent=getattr(agent, "id", None),
                 name=name, granted=True)
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


# --- encrypted-file backend (E1d) ------------------------------------------
# At-rest encryption via the system `openssl` binary (aes-256-cbc + pbkdf2,
# passphrase from $JIGGA_SECRETS_PASSPHRASE — set it in the service unit).
# The design doc named `age`, but age refuses non-interactive passphrases (tty
# only), which a supervisor can't provide; openssl accepts pass via env and is
# ubiquitous. Same principle holds: no homegrown crypto, shell-out only.

_PASSPHRASE_ENV = "JIGGA_SECRETS_PASSPHRASE"
_ENC_SUFFIX = ".enc"


def _passphrase() -> str:
    value = os.environ.get(_PASSPHRASE_ENV, "")
    if not value:
        raise ValueError(
            f"secrets.backend=encrypted-file needs {_PASSPHRASE_ENV} in the environment "
            "(set it in the supervisor service unit, or export it for CLI use).")
    return value


def _openssl(args: list[str], data: bytes) -> bytes:
    _passphrase()
    if shutil.which("openssl") is None:
        raise ValueError("secrets.backend=encrypted-file requires the `openssl` binary.")
    result = subprocess.run(["openssl", "enc", *args, "-pbkdf2", "-pass", f"env:{_PASSPHRASE_ENV}"],
                            input=data, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError("openssl failed (wrong passphrase?): "
                           + result.stderr.decode(errors="replace")[:200])
    return result.stdout


def _enc_get(home: Path, name: str) -> str | None:
    path = _file_path(home, name + _ENC_SUFFIX)
    if not path.exists():
        plain = _file_path(home, name)  # migration fallthrough: pre-E1d secret
        return plain.read_text(encoding="utf-8") if plain.exists() else None
    return _openssl(["-d", "-aes-256-cbc"], path.read_bytes()).decode("utf-8")


def _enc_set(home: Path, name: str, value: str) -> str:
    path = _file_path(home, name + _ENC_SUFFIX)
    ensure_dir(path.parent)
    path.write_bytes(_openssl(["-aes-256-cbc"], value.encode("utf-8")))
    path.chmod(0o600)
    _file_path(home, name).unlink(missing_ok=True)  # never leave a stale plaintext twin
    return str(path)


def migrate_to_encrypted(home: Path) -> list[str]:
    """Encrypt every plaintext secret in place (plaintext removed after each
    successful write) and select the backend in config. Idempotent."""
    from jigga.core.io import read_yaml, write_yaml

    _passphrase()  # fail before touching anything if the passphrase is unset
    migrated = []
    secrets_dir = Path(home) / "secrets"
    for path in sorted(secrets_dir.iterdir()) if secrets_dir.exists() else []:
        if path.is_file() and not path.name.startswith(".") and not path.name.endswith(_ENC_SUFFIX):
            _enc_set(home, path.name, path.read_text(encoding="utf-8"))
            migrated.append(path.name)
    config_path = Path(home) / "config.yaml"
    config = read_yaml(config_path) if config_path.exists() else {}
    config["secrets"] = {**(config.get("secrets") or {}), "backend": "encrypted-file"}
    write_yaml(config_path, config)
    return migrated


def _validate(name: str) -> str:
    if not name or not _NAME.match(name) or name.startswith("."):
        raise ValueError(f"Invalid secret name: {name!r} (letters, digits, . _ - only)")
    return name


def _file_path(home: Path, name: str) -> Path:
    return Path(home) / "secrets" / name


def _env_var(name: str) -> str:
    return "JIGGA_SECRET_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def team_of(home: Path, agent: Any) -> str | None:
    """The team an agent belongs to, or None if it belongs to none."""
    from jigga.core.config import load_teams

    agent_id = getattr(agent, "id", None)
    if not agent_id:
        return None
    try:
        teams = load_teams(Path(home) / "teams")
    except Exception:  # noqa: BLE001 — an unreadable team file must not block a secret read
        return None
    for team_id, team in sorted(teams.items()):
        for member in team.agents or []:
            member_id = member.get("id") if isinstance(member, dict) else member
            if member_id == agent_id:
                return team_id
    return None


def resolve_secret_name(home: Path, name: str, agent: Any) -> str:
    """The physical secret name to read for `name` on behalf of `agent`.

    Two tenants on one runtime need *different* accounts behind the same
    logical secret — Oakwood and Driftwood each have their own Postiz login,
    and a handler asking for `postiz_api_key` must not get the other venue's.
    Before this, the namespace was flat and handlers resolved literal names, so
    the only way to serve a second tenant was to fork the capability. That is
    exactly the "a second customer arrives as a fork" failure the field lessons
    warn about.

    Resolution is team-scoped and falls back: `<name>@<team>` if it exists,
    else `<name>`. So a single-tenant install needs no change at all, and a
    tenant opts in simply by storing a scoped secret.

    The isolation here is *structural*, not a grant. An agent's team decides
    which value it sees, so an Oakwood agent cannot reach Driftwood's
    credential even if someone grants it the logical name — which is the
    property that makes mutually-exclusive tenants safe on one box.
    """
    if _TEAM_SEPARATOR in name:
        return name  # already explicit; caller asked for a specific tenant
    team = team_of(home, agent)
    if not team:
        return name
    scoped = f"{name}{_TEAM_SEPARATOR}{team}"
    return scoped if _read_backend(home, scoped) is not None else name


def _read_backend(home: Path, name: str) -> str | None:
    """Read a stored secret by its physical name. No policy, no scoping."""
    backend = _backend(home)
    if backend == "env":
        return os.environ.get(_env_var(name))
    if backend == "encrypted-file":
        return _enc_get(Path(home), name)
    if backend == "keychain":
        value = _keychain_get(name)
        if value is not None:
            return value
        # Fall through to file: pre-keychain secrets keep working, and
        # `jigga secrets set` on this backend migrates them forward.
    path = _file_path(home, name)
    return path.read_text(encoding="utf-8") if path.exists() else None


def get_secret(home: Path, name: str) -> str | None:
    """The secret's value, or None when unset. All runtime secret reads route
    here — never open files under secrets/ directly. Inside a capability
    invocation, the executing agent's `permissions.secrets` grant is enforced
    before anything is released (E1c), and the name is resolved against the
    agent's team so two tenants can hold different values for one logical
    secret."""
    _validate(name)
    ctx = _capability_ctx.get()
    if ctx is not None and ctx[0] is not None:
        # Enforced on the LOGICAL name the capability asked for. Team scoping is
        # automatic, so a grant is written once (`postiz_api_key`) rather than
        # per tenant — and cannot be used to reach across teams either way.
        _enforce_grant(Path(home), name, ctx[0], ctx[1])
        name = resolve_secret_name(Path(home), name, ctx[0])
    return _read_backend(home, name)


def set_secret(home: Path, name: str, value: str) -> str:
    """Store a secret; returns a display location (never the value)."""
    _validate(name)
    backend = _backend(home)
    if backend == "env":
        raise ValueError(f"secrets.backend=env is read-only — export {_env_var(name)} instead.")
    if backend == "encrypted-file":
        return _enc_set(Path(home), name, value)
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
    enc = _file_path(home, name + _ENC_SUFFIX)
    if enc.exists():
        enc.unlink()
        deleted = True
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
    if not secrets_dir.exists():
        return []
    names = {p.name[:-len(_ENC_SUFFIX)] if p.name.endswith(_ENC_SUFFIX) else p.name
             for p in secrets_dir.iterdir() if p.is_file() and not p.name.startswith(".")}
    return sorted(names)

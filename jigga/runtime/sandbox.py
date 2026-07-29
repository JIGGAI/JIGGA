"""Shared subprocess sandbox primitives.

JIGGA spawns subprocesses from a handful of places today (`codex_cli` and
`claude_code` subagent backends, `mcp_server` capability handler) and will
grow more (browser tools, real shell runner, etc.). Each spawner historically
duplicated the env-allowlist + restricted-cwd + timeout pattern. This module
centralizes it so the next spawner inherits it for free, and so the single
seam exists when OS-level isolation (firejail, bwrap, container, etc.) becomes
a real requirement.

What this module is:
- A `SandboxSpec` dataclass describing the bounded subprocess invocation.
- `build_restricted_env(secrets_required)` — env allowlist filter.
- `run_sandboxed(spec, input=...)` — subprocess.run wrapper that applies the
  spec uniformly (env, cwd, timeout, captured stdio).

What this module is NOT (yet):
- It does not implement OS-level sandboxing. It only restricts the env and
  cwd surface that a subprocess receives. Real isolation lives behind the
  `run_sandboxed` seam — a future patch can swap the subprocess invocation
  for `firejail`/`bwrap`/`docker run` without touching callers.
- It does not include audit emission. Each caller continues to emit its own
  domain-specific lifecycle events (`subagent.spawn.*`, `capability.invocation.*`)
  since the audit payload needs caller-specific context (session_id, run_id, etc.).

Routing rule — what goes through this module and what doesn't:

  External CLIs that act on the agent's behalf with their own credentials
  (codex, claude, MCP servers, future shell runner, future headless browser)
  → MUST use run_sandboxed.

  Local UX tools that need the user's session env to function
  (notify-send, osascript display notifications, future tray-icon helpers)
  → MUST NOT use run_sandboxed.

The reason: notification daemons and other local UX rely on `DISPLAY`,
`WAYLAND_DISPLAY`, `XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS`, etc. —
exactly the env vars `build_restricted_env` strips. Sandboxing them buys no
security (they don't act with the agent's authority on external systems) and
breaks delivery.

If you're about to spawn something and aren't sure which side it falls on,
ask: "does this process act with the agent's authority on external systems,
or just render output to the user's desktop?" Authority side: sandbox.
Render side: don't.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# Env vars passed through to every sandboxed subprocess by default. Chosen to
# keep basic locale + shell-tool resolution working without leaking caller
# secrets. Anything outside this set must be explicitly opted in via the
# capability/manifest declaring `permissions.secrets.required`.
BASE_ENV_ALLOWLIST: frozenset[str] = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TERM"})


@dataclass(frozen=True)
class SandboxSpec:
    """Bounded subprocess invocation.

    `command` + `args` form the argv; `cwd` is the working directory; the env
    is built from BASE_ENV_ALLOWLIST plus any name in `secrets_required` that
    the caller's environment actually exports. Missing secrets are silently
    omitted — callers that need to fail-fast on a missing key should check
    the returned env or os.environ themselves before calling run_sandboxed.
    """

    command: str
    args: list[str] = field(default_factory=list)
    cwd: Path = field(default_factory=Path.cwd)
    secrets_required: list[str] = field(default_factory=list)
    timeout_seconds: float = 30.0
    # E2a (Milestone E): OS-sandbox hints, honored when the bwrap backend is
    # active. `fs_read`/`fs_write` are extra binds beyond the defaults (system
    # dirs read-only, cwd read-write, tmpfs /tmp). `network=False` unshares the
    # network namespace entirely — the strongest egress bound we have.
    fs_read: list[Path] = field(default_factory=list)
    fs_write: list[Path] = field(default_factory=list)
    network: bool = True
    # Escape hatch surfaced (and warned about) by the capability scanner.
    sandbox: bool = True
    # E3a: when set (and network is True), a per-invocation localhost proxy is
    # started and HTTP(S)_PROXY injected — the subprocess can only reach these
    # hosts over HTTP(S). None = no proxy (unrestricted within the backend's
    # network namespace). `logs_dir`/`label` feed the egress audit events.
    egress_allow: list[str] | None = None
    logs_dir: Path | None = None
    label: str | None = None
    # Explicit env key=value pairs injected into the subprocess, merged OVER
    # the allowlisted env. Unlike `secrets_required` (which only passes through
    # values already in os.environ), `extra_env` lets a caller supply a value
    # the parent process holds but doesn't export — e.g. a keyring password
    # JIGGA reads from its own secrets dir and hands to gogcli as
    # GOG_KEYRING_PASSWORD. Keep these to non-sensitive switches + secrets the
    # caller has already gated; they are written into the child's environment.
    extra_env: dict[str, str] = field(default_factory=dict)


def build_restricted_env(
    secrets_required: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a filtered env dict containing only the base allowlist plus any
    secret names the caller has explicitly requested, then merged with any
    explicit `extra_env` key=value pairs (which take precedence). Unrequested
    env vars — including unrelated API keys, tokens, and AWS creds that may be
    exported in the caller's shell — are excluded."""
    allowed = set(BASE_ENV_ALLOWLIST)
    if secrets_required:
        allowed.update(str(name) for name in secrets_required)
    env = {key: value for key, value in os.environ.items() if key in allowed}
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})
    return env


# System paths mounted read-only under bwrap so subprocesses can exec at all.
_SYSTEM_RO = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt")


def sandbox_backend(home: Path | None = None) -> str:
    """Resolved OS-sandbox backend: `none` (default until E2c flips auto) or
    `bwrap`. Explicit `bwrap` without the binary is a loud error — a user who
    turned the sandbox on must never silently run unsandboxed."""
    import shutil

    from jigga.core.config import load_runtime_config
    from jigga.core.paths import resolve_home

    config = load_runtime_config(resolve_home(home)).get("sandbox") or {}
    backend = str(config.get("backend") or "auto")
    if backend == "auto":
        return "none"  # E2c flips this to bwrap-when-available after prod soak
    if backend == "bwrap" and shutil.which("bwrap") is None:
        raise RuntimeError("sandbox.backend=bwrap but the `bwrap` binary is not installed "
                           "(apt install bubblewrap), and silently degrading would betray "
                           "an explicit setting.")
    return backend


def bwrap_argv(spec: SandboxSpec, env: dict[str, str]) -> list[str]:
    """The bwrap prefix for a spec: deny-by-default filesystem (system dirs
    ro, cwd rw, declared binds only), cleared env re-set from the restricted
    dict (kernel-enforced, not dict-filtered), fresh /tmp, optional network
    unshare, dies with the parent."""
    argv = ["bwrap", "--die-with-parent", "--clearenv", "--proc", "/proc",
            "--dev", "/dev", "--tmpfs", "/tmp", "--unshare-pid", "--unshare-ipc"]
    if not spec.network:
        argv.append("--unshare-net")
    for path in _SYSTEM_RO:
        if Path(path).exists():
            argv += ["--ro-bind", path, path]
    for path in spec.fs_read:
        argv += ["--ro-bind", str(path), str(path)]
    seen_rw = set()
    for path in [spec.cwd, *spec.fs_write]:
        raw = str(path)
        if raw not in seen_rw:
            argv += ["--bind", raw, raw]
            seen_rw.add(raw)
    for key, value in sorted(env.items()):
        argv += ["--setenv", key, value]
    argv += ["--chdir", str(spec.cwd)]
    return argv


def run_sandboxed(
    spec: SandboxSpec,
    *,
    input: str | None = None,
    home: Path | None = None,
) -> subprocess.CompletedProcess:
    """Spawn the subprocess described by `spec` and wait for completion.

    With `sandbox.backend: bwrap` (and `spec.sandbox` true), the argv is
    prefixed with a bubblewrap invocation so the env/filesystem/network bounds
    are kernel-enforced instead of merely dict-filtered. The returned
    `CompletedProcess` carries `stdout`/`stderr` (text mode) and `returncode`.
    Raises `subprocess.TimeoutExpired` if the spec's timeout elapses; callers
    decide how to surface the failure.
    """
    env = build_restricted_env(spec.secrets_required, spec.extra_env)
    proxy = None
    if spec.network and spec.egress_allow is not None:
        from jigga.runtime.egress_proxy import EgressProxy

        proxy = EgressProxy(spec.egress_allow, logs_dir=spec.logs_dir, label=spec.label)
        port = proxy.start()
        proxy_url = f"http://127.0.0.1:{port}"
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[key] = proxy_url
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    argv = [spec.command, *spec.args]
    if spec.sandbox and sandbox_backend(home) == "bwrap":
        argv = bwrap_argv(spec, env) + ["--"] + argv
    try:
        return subprocess.run(
            argv,
            input=input,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(spec.cwd),
            timeout=spec.timeout_seconds,
            check=False,
        )
    finally:
        if proxy is not None:
            proxy.stop()

"""Shared subprocess sandbox primitives.

JIGGA spawns subprocesses from two places today (`codex_cli` subagent backend
and `mcp_server` capability handler) and is likely to grow more (claude_code
adapter, browser tools, real notification senders). Each spawner historically
duplicated the env-allowlist + restricted-cwd + timeout pattern. This module
centralizes that pattern so the next spawner inherits it for free, and so the
single seam exists when OS-level isolation (firejail, bwrap, container, etc.)
becomes a real requirement.

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


def build_restricted_env(secrets_required: list[str] | None = None) -> dict[str, str]:
    """Return a filtered env dict containing only the base allowlist plus any
    secret names the caller has explicitly requested. Unrequested env vars —
    including unrelated API keys, tokens, and AWS creds that may be exported
    in the caller's shell — are excluded."""
    allowed = set(BASE_ENV_ALLOWLIST)
    if secrets_required:
        allowed.update(str(name) for name in secrets_required)
    return {key: value for key, value in os.environ.items() if key in allowed}


def run_sandboxed(
    spec: SandboxSpec,
    *,
    input: str | None = None,
) -> subprocess.CompletedProcess:
    """Spawn the subprocess described by `spec` and wait for completion.

    The returned `CompletedProcess` carries `stdout`/`stderr` (text mode) and
    `returncode`. Raises `subprocess.TimeoutExpired` if the spec's timeout
    elapses; callers decide how to surface the failure.
    """
    return subprocess.run(
        [spec.command, *spec.args],
        input=input,
        capture_output=True,
        text=True,
        env=build_restricted_env(spec.secrets_required),
        cwd=str(spec.cwd),
        timeout=spec.timeout_seconds,
        check=False,
    )

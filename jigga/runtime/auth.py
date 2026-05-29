"""External CLI backend authentication helpers.

JIGGA spawns `codex` (Codex CLI) and `claude` (Claude Code CLI) as subagent
backends. Both have their own OAuth login flow that stores credentials on
disk under the user's `$HOME` (`~/.codex/`, `~/.claude/`). Because the sandbox
allowlist already passes `HOME` through to the spawned subprocesses, the
subagent backends just work once the user has logged in to the upstream CLI.

This module's only job is to make that one-time login easy to discover and
verify from inside JIGGA's UX — wrap the upstream `<cli> login` command and
report which backends are installed.

Per the subprocess routing rule in `runtime.sandbox`, the login subprocess is
a render-side process (it needs the user's TTY, browser env, etc.) so it does
NOT go through `run_sandboxed`. It inherits the user's full environment and
attaches to the user's stdin/stdout/stderr so the OAuth URL and prompts are
visible.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

SUPPORTED_BACKENDS: dict[str, dict[str, Any]] = {
    "codex_cli": {
        "binary": "codex",
        "login_args": ["login"],
        "config_dir": "~/.codex",
        "url": "https://platform.openai.com/docs/codex",
    },
    "claude_code": {
        "binary": "claude",
        "login_args": ["login"],
        "config_dir": "~/.claude",
        "url": "https://docs.claude.com/en/docs/claude-code",
    },
}


@dataclass(frozen=True)
class BackendAuthStatus:
    backend: str
    binary: str
    binary_available: bool
    binary_path: str | None
    config_dir: str
    install_url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "binary": self.binary,
            "available": self.binary_available,
            "path": self.binary_path,
            "config_dir": self.config_dir,
            "install_url": self.install_url,
        }


def auth_status() -> list[BackendAuthStatus]:
    """Report installed/unsinstalled state for each supported external backend.

    Only checks binary presence on PATH. Doesn't try to validate the upstream
    auth token (that would require parsing each CLI's private config format,
    which is brittle); we let `<cli> login` and the subagent dispatch itself
    surface real auth failures.
    """
    statuses: list[BackendAuthStatus] = []
    for backend, spec in SUPPORTED_BACKENDS.items():
        binary = str(spec["binary"])
        path = shutil.which(binary)
        statuses.append(
            BackendAuthStatus(
                backend=backend,
                binary=binary,
                binary_available=path is not None,
                binary_path=path,
                config_dir=str(spec["config_dir"]),
                install_url=str(spec["url"]),
            )
        )
    return statuses


def run_external_login(backend: str) -> int:
    """Invoke the upstream `<cli> login` command interactively.

    The subprocess inherits the user's TTY and full environment — login flows
    open a browser, prompt for confirmation, and write tokens to disk. Returns
    the subprocess exit code. Raises `ValueError` for unknown backends and
    `FileNotFoundError` when the binary isn't installed.
    """
    if backend not in SUPPORTED_BACKENDS:
        allowed = ", ".join(sorted(SUPPORTED_BACKENDS))
        raise ValueError(f"Unknown auth backend: {backend!r}. Supported: {allowed}.")
    spec = SUPPORTED_BACKENDS[backend]
    binary = str(spec["binary"])
    if shutil.which(binary) is None:
        raise FileNotFoundError(
            f"{binary!r} CLI is not installed or not on PATH. "
            f"See {spec['url']} for install instructions."
        )
    # Intentionally NOT through runtime.sandbox.run_sandboxed — login is a
    # render-side process needing the user's TTY, browser env, etc.
    completed = subprocess.run([binary, *spec["login_args"]], check=False)
    return completed.returncode

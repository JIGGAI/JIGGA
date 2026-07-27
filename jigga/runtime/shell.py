"""Shell capability — `shell.run`, dispatched through `tools/safe_process.py`.

The safe-process runner has existed since the MVP (list-argv only, never
`shell=True`, dangerous-pattern deny, per-agent shell/cwd policy, artifacts on
disk) but was never wired into capability dispatch — agents couldn't run a
command at all. This closes that gap without loosening anything:

- The capability is `risk_level: high`: outside `autonomous` mode every call
  parks for approval (`approve <code>`), *and* the agent's own `shell` policy
  (deny by default / `restricted` allowlist / `ask`) is enforced inside the
  runner — approval never bypasses a policy deny.
- Commands are argv lists (a string input is shlex-split, never a shell).
  Pipes/redirection/expansion don't exist here by construction.
- cwd defaults to the runtime home and is itself policy-checked
  (`filesystem execute`). Full stdout/stderr land as run artifacts; the model
  sees a truncated inline copy.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from jigga.tools.safe_process import run_safe_process

_INLINE_OUTPUT_LIMIT = 6_000
_DEFAULT_TIMEOUT = 60
_MAX_TIMEOUT = 600


def shell_handler(step, _capability, resolved_input, _memory_context, runtime) -> Any:
    if step.action != "shell.run":
        raise ValueError(f"Unknown shell action: {step.action}")
    if runtime.agent is None:
        raise ValueError("shell.run requires an executing agent")
    data = resolved_input if isinstance(resolved_input, dict) else {}

    raw = data.get("command")
    if isinstance(raw, str):
        command = shlex.split(raw)
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        command = list(raw)
    else:
        raise ValueError("shell.run requires input.command (string or argv list)")
    if not command:
        raise ValueError("shell.run requires a non-empty command")

    cwd = Path(str(data.get("cwd") or runtime.home)).expanduser()
    timeout = min(int(data.get("timeout") or _DEFAULT_TIMEOUT), _MAX_TIMEOUT)

    record = run_safe_process(
        runtime.agent, command, cwd,
        artifacts_dir=Path(runtime.home) / "runs" / "processes",
        timeout_seconds=timeout,
        apply=True,
    )
    result = {"source": "capability.shell", **{k: record[k] for k in (
        "id", "command", "cwd", "status", "returncode", "stdout", "stderr")
        if record.get(k) is not None}}
    # Inline (truncated) output so the model can read it without another hop;
    # the full files remain as artifacts.
    for stream in ("stdout", "stderr"):
        path = record.get(stream)
        if path and Path(path).exists():
            text = Path(path).read_text(encoding="utf-8")
            result[f"{stream}_text"] = text[:_INLINE_OUTPUT_LIMIT]
            if len(text) > _INLINE_OUTPUT_LIMIT:
                result[f"{stream}_truncated"] = True
    return result

"""Native filesystem capability handler.

The first capability where the runtime touches the user's filesystem on the
agent's behalf. Four actions exposed to workflows:

  - filesystem.read_file       — return file content (text, size-capped)
  - filesystem.write_file      — write text content (won't overwrite without opt-in)
  - filesystem.list_directory  — directory listing (optional recursive + glob)
  - filesystem.search_files    — regex grep across files under a path

Every action resolves the target path through `Path.expanduser()`, then calls
`evaluate_filesystem(runtime.agent, resolved_path, operation)` against the
*executing agent's* policy at runtime. Plan-time only checks the capability's
declared paths (which are intentionally empty — paths come from step input);
the runtime per-action policy check is what makes the capability safe to
ship as bundled.

Per the subprocess routing rule in `runtime.sandbox`, this handler does NO
subprocess spawning — it's pure Python file I/O against the user's filesystem
under the agent's permission scope. It's not a render-side process and not a
sandboxed-authority process; it's a third category: native action against the
local filesystem, gated by the policy layer.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir
from jigga.core.models import WorkflowStep
from jigga.runtime.capabilities import CapabilityManifest
from jigga.runtime.policy import evaluate_filesystem

# Read cap protects against accidentally loading a massive binary into a
# workflow's outputs dict. 1MB is enough for any reasonable text artifact
# (the runtime is targeting personal AI workers, not log forensics).
MAX_READ_BYTES = 1_048_576

# Directory listing cap — protects against listing a deep ~/Library tree
# and ending up with a 50MB JSON in the run artifact.
MAX_LIST_ENTRIES = 1_000

# Search defaults. Workflows can override max_matches via step input up to
# MAX_LIST_ENTRIES (any higher would defeat the purpose of the cap).
DEFAULT_SEARCH_MAX_MATCHES = 100


class FilesystemPolicyError(RuntimeError):
    """Raised when a filesystem action is denied by the agent's policy at
    runtime. Distinct from ValueError so workflow audit can mark it
    specifically as `policy.denied` rather than generic step failure."""


def _require_path(input_value: Any, action: str) -> str:
    if not isinstance(input_value, dict):
        raise ValueError(f"{action} requires a dict input with 'path'")
    raw = input_value.get("path")
    if not raw or not isinstance(raw, str):
        raise ValueError(f"{action} requires non-empty 'path' string in input")
    return str(Path(raw).expanduser())


def _check_policy(runtime, path: str, operation: str, action: str) -> None:
    if runtime.agent is None:
        raise ValueError(f"{action} requires an executing agent")
    decision = evaluate_filesystem(runtime.agent, path, operation=operation)
    if decision.status != "allow":
        raise FilesystemPolicyError(
            f"Agent {runtime.agent.id} cannot {operation} {path}: "
            f"{decision.reason or 'not permitted by agent policy'}"
        )


def _read_file(
    step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime,
) -> Any:
    path = _require_path(resolved_input, step.action)
    _check_policy(runtime, path, "read", step.action)
    file_path = Path(path)
    if not file_path.exists():
        return {
            "source": "capability.filesystem",
            "action": step.action,
            "path": path,
            "exists": False,
            "content": None,
        }
    if file_path.is_dir():
        raise ValueError(f"{step.action}: {path} is a directory, not a file")
    size = file_path.stat().st_size
    if size > MAX_READ_BYTES:
        raise ValueError(
            f"{step.action}: {path} is {size} bytes; exceeds cap of {MAX_READ_BYTES}"
        )
    content = file_path.read_text(encoding="utf-8", errors="replace")
    return {
        "source": "capability.filesystem",
        "action": step.action,
        "path": path,
        "exists": True,
        "size": size,
        "content": content,
    }


def _write_file(
    step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime,
) -> Any:
    if not isinstance(resolved_input, dict):
        raise ValueError(f"{step.action} requires a dict input with 'path' and 'content'")
    path = _require_path(resolved_input, step.action)
    content = resolved_input.get("content")
    if content is None:
        raise ValueError(f"{step.action} requires 'content' in input")
    if not isinstance(content, str):
        content = str(content)
    overwrite = bool(resolved_input.get("overwrite", False))
    create_parents = bool(resolved_input.get("create_parents", False))

    _check_policy(runtime, path, "write", step.action)

    file_path = Path(path)
    if file_path.exists() and not overwrite:
        raise ValueError(
            f"{step.action}: {path} already exists; pass overwrite: true to replace"
        )
    if create_parents:
        ensure_dir(file_path.parent)
    elif not file_path.parent.exists():
        raise ValueError(
            f"{step.action}: parent directory {file_path.parent} does not exist; "
            "pass create_parents: true to create it"
        )
    bytes_written = file_path.write_text(content, encoding="utf-8")
    return {
        "source": "capability.filesystem",
        "action": step.action,
        "path": path,
        "bytes_written": bytes_written,
        "overwrote": file_path.exists() and overwrite,
    }


def _list_directory(
    step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime,
) -> Any:
    path = _require_path(resolved_input, step.action)
    _check_policy(runtime, path, "read", step.action)
    dir_path = Path(path)
    if not dir_path.exists():
        return {
            "source": "capability.filesystem",
            "action": step.action,
            "path": path,
            "exists": False,
            "entries": [],
        }
    if not dir_path.is_dir():
        raise ValueError(f"{step.action}: {path} is not a directory")
    recursive = bool(resolved_input.get("recursive", False))
    glob = resolved_input.get("glob")
    iterator = dir_path.rglob("*") if recursive else dir_path.iterdir()
    entries: list[dict[str, Any]] = []
    truncated = False
    for child in iterator:
        if glob and not fnmatch.fnmatch(child.name, str(glob)):
            continue
        if len(entries) >= MAX_LIST_ENTRIES:
            truncated = True
            break
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "is_dir": child.is_dir(),
                "size": child.stat().st_size if child.is_file() else None,
            }
        )
    return {
        "source": "capability.filesystem",
        "action": step.action,
        "path": path,
        "exists": True,
        "recursive": recursive,
        "glob": glob,
        "entries": entries,
        "truncated": truncated,
    }


def _search_files(
    step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime,
) -> Any:
    if not isinstance(resolved_input, dict):
        raise ValueError(f"{step.action} requires a dict input with 'path' and 'pattern'")
    path = _require_path(resolved_input, step.action)
    pattern = resolved_input.get("pattern")
    if not pattern or not isinstance(pattern, str):
        raise ValueError(f"{step.action} requires non-empty 'pattern' string in input")
    case_sensitive = bool(resolved_input.get("case_sensitive", False))
    raw_max = resolved_input.get("max_matches", DEFAULT_SEARCH_MAX_MATCHES)
    try:
        max_matches = max(1, min(int(raw_max), MAX_LIST_ENTRIES))
    except (TypeError, ValueError):
        max_matches = DEFAULT_SEARCH_MAX_MATCHES

    _check_policy(runtime, path, "read", step.action)
    root = Path(path)
    if not root.exists():
        return {
            "source": "capability.filesystem",
            "action": step.action,
            "path": path,
            "pattern": pattern,
            "matches": [],
            "exists": False,
        }
    try:
        regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"{step.action}: invalid regex {pattern!r}: {exc}") from exc

    matches: list[dict[str, Any]] = []
    truncated = False
    iterator = [root] if root.is_file() else root.rglob("*")
    for file_path in iterator:
        if not file_path.is_file():
            continue
        try:
            if file_path.stat().st_size > MAX_READ_BYTES:
                continue
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(
                    {
                        "path": str(file_path),
                        "line_number": line_number,
                        "line": line.rstrip("\n"),
                    }
                )
                if len(matches) >= max_matches:
                    truncated = True
                    break
        if truncated:
            break

    return {
        "source": "capability.filesystem",
        "action": step.action,
        "path": path,
        "pattern": pattern,
        "case_sensitive": case_sensitive,
        "matches": matches,
        "truncated": truncated,
    }


ACTION_HANDLERS = {
    "filesystem.read_file": _read_file,
    "filesystem.write_file": _write_file,
    "filesystem.list_directory": _list_directory,
    "filesystem.search_files": _search_files,
}


def filesystem_handler(
    step: WorkflowStep,
    capability: CapabilityManifest,
    resolved_input: Any,
    memory_context: dict[str, Any],
    runtime,
) -> Any:
    """Dispatch by step.action to the right per-action handler.

    Single entrypoint registered as `runtime.filesystem` in dispatcher.HANDLERS;
    the action selector is `step.action` (matches the pattern used by
    `_calendar_handler` etc.) since one capability declares all four actions.
    """
    handler = ACTION_HANDLERS.get(step.action)
    if handler is None:
        raise ValueError(
            f"Unknown filesystem action: {step.action!r}. "
            f"Supported: {', '.join(sorted(ACTION_HANDLERS))}."
        )
    return handler(step, capability, resolved_input, memory_context, runtime)

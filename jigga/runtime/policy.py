from __future__ import annotations

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jigga.core.models import AgentConfig, WorkflowStep

PolicyStatus = Literal["allow", "deny", "ask"]

# Modes that should prevent any execution that calls a model, runs a tool, or
# mutates state. `plan_only` and `locked_down` agents are read-only in practice.
NON_EXECUTING_MODES = frozenset({"plan_only", "locked_down"})
# Modes that require approval before each consequential action.
APPROVAL_MODES = frozenset({"ask"})


@dataclass(frozen=True)
class PolicyDecision:
    status: PolicyStatus
    reason: str | None = None
    permission: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"status": self.status, "reason": self.reason, "permission": self.permission}


# Kept as the fallback scan for a command line that cannot be lexed (unbalanced
# quotes). Matching these as raw substrings is what `is_dangerous_command`
# replaces: "dd " lives inside "git add ", and "rm " inside "terraform ", so the
# substring form denied `git add .` for every agent in every mode — including
# `allow` — and no configuration could grant around it.
DANGEROUS_SHELL_PATTERNS = (
    "rm ",
    "rm -",
    "sudo ",
    "mkfs",
    "dd ",
    ":(){",
    "chmod -R 777",
    "chown -R",
    "> /dev/",
    "curl *|*sh",
    "wget *|*sh",
)

# Programs that are never safe to run, whatever the arguments. Compared against
# the command NAME — argv[0]'s basename — never against the whole line.
_DANGEROUS_PROGRAMS = frozenset({"rm", "sudo", "dd"})
# `mkfs`, `mkfs.ext4`, `mkfs.xfs`, …
_DANGEROUS_PROGRAM_PREFIXES = ("mkfs",)
# Prefixes that run another program: look past them, so `xargs rm -rf /` and
# `env FOO=1 rm -rf /` are still caught.
_COMMAND_WRAPPERS = frozenset({
    "env", "nohup", "nice", "ionice", "time", "timeout", "stdbuf",
    "xargs", "command", "exec", "setsid", "builtin",
})
_SHELL_PROGRAMS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
_DOWNLOADERS = frozenset({"curl", "wget"})
# Character-only tokens the lexer emits for shell operators.
_OPERATOR_CHARS = frozenset("|&;()<>")
# Writing to these is routine; writing to any other /dev node is not.
_SAFE_DEVICES = frozenset({
    "null", "zero", "full", "tty", "stdin", "stdout", "stderr", "fd",
    "random", "urandom",
})
_DEVICE_WRITE = re.compile(r">>?\s*/dev/([A-Za-z0-9_]+)")
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{")


def _command_segments(command: str) -> list[list[str]] | None:
    """Split a command line into argv segments at shell operators. `None` when
    the line cannot be lexed, so the caller falls back to the substring scan.

    `safe_process` hands us `" ".join(argv)` from a list that is never run
    through a shell, so in practice there is one segment. Splitting anyway keeps
    the check honest for the free-form strings other callers pass.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and set(token) <= _OPERATOR_CHARS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _program_of(argv: list[str]) -> tuple[str, list[str]]:
    """The program a segment actually runs (basename, lowercased) and its
    arguments, looking past wrapper commands and their `VAR=value` prefixes."""
    index = 0
    while index < len(argv):
        name = os.path.basename(argv[index]).lower()
        if name not in _COMMAND_WRAPPERS:
            return name, argv[index + 1:]
        index += 1
        while index < len(argv) and (argv[index].startswith("-") or "=" in argv[index]):
            index += 1
    return "", []


def _has_recursive_flag(args: list[str]) -> bool:
    for arg in args:
        if arg == "--recursive":
            return True
        if arg.startswith("-") and not arg.startswith("--") and "r" in arg.lower():
            return True
    return False


def _segment_is_dangerous(argv: list[str]) -> bool:
    program, args = _program_of(argv)
    if not program:
        return False
    if program in _DANGEROUS_PROGRAMS or program.startswith(_DANGEROUS_PROGRAM_PREFIXES):
        return True
    if program == "chmod" and _has_recursive_flag(args) and any("777" in a for a in args):
        return True
    if program == "chown" and _has_recursive_flag(args):
        return True
    return False


def is_dangerous_command(command: str) -> bool:
    """Whether a command is refused outright, before any mode or allow-list is
    consulted. Matched on program names and shell structure rather than raw
    substrings, so `git add .`, `terraform apply` and `npm test > /dev/null`
    are ordinary commands again while `rm -rf /` and `sudo …` stay refused."""
    if _FORK_BOMB.search(command):
        return True
    for device in _DEVICE_WRITE.findall(command):
        if device.lower() not in _SAFE_DEVICES:
            return True
    segments = _command_segments(command)
    if segments is None:  # unlexable — fall back to the blunt scan
        lowered = command.lower()
        return any(fnmatch.fnmatch(lowered, p) or p in lowered for p in DANGEROUS_SHELL_PATTERNS)
    downloaded = False
    for argv in segments:
        if _segment_is_dangerous(argv):
            return True
        program, _ = _program_of(argv)
        if downloaded and program in _SHELL_PROGRAMS:
            return True  # curl … | sh
        if program in _DOWNLOADERS:
            downloaded = True
    return False


def _mode(config: dict[str, Any] | None, default: str = "deny") -> str:
    if not isinstance(config, dict):
        return default
    value = config.get("mode", default)
    return str(value or default)


def _matches_any(value: str, patterns: list[str]) -> bool:
    # Canonicalize `..`/`.` segments before matching. Without this a model-supplied
    # path like `<allowed>/../../etc/passwd` matches an allow rule lexically while
    # actually resolving outside it — a permission-gate bypass (both allow escape
    # and deny evasion). normpath is lexical (no FS access, works on missing paths).
    raw = os.path.normpath(str(Path(value).expanduser()))
    raw_parts = Path(raw).parts
    return any(_path_matches(raw, raw_parts, pattern) for pattern in patterns)


def _path_matches(raw: str, raw_parts: tuple[str, ...], pattern: str) -> bool:
    has_glob = any(token in pattern for token in "*?[")
    # Bare basename pattern (no glob, no separator) — match any path segment.
    # This is how rules like `.env` or `id_rsa` should behave: they deny the
    # file wherever it appears in the tree, not only at the workspace root.
    if not has_glob and "/" not in pattern:
        return pattern in raw_parts

    expanded = str(Path(pattern).expanduser())

    # `<prefix>/**` — match recursively below `<prefix>` wherever it appears.
    # For relative prefixes this is gitignore-style "any depth"; for absolute
    # prefixes (starting with / or ~) the prefix anchors at the root.
    if expanded.endswith("/**"):
        head_parts = Path(expanded[:-3]).parts
        if not head_parts:
            return True
        for i in range(len(raw_parts) - len(head_parts) + 1):
            if raw_parts[i : i + len(head_parts)] == head_parts and i + len(head_parts) < len(raw_parts):
                return True
        return False

    # Other glob patterns — fnmatch against the full expanded path.
    if has_glob:
        return fnmatch.fnmatch(raw, expanded)

    # Plain path — exact match or directory prefix.
    normalized = expanded.rstrip("/")
    return raw == normalized or raw.startswith(normalized + "/")


def resolve_permission_mode(agent: AgentConfig | None, default_mode: str) -> str:
    if agent is None or agent.permission_mode is None:
        return default_mode
    return agent.permission_mode


def granted_actions(agent: AgentConfig) -> list[str]:
    """Every action explicitly granted to an agent: its `tools:` list plus any
    `permissions.tools.allow`. Order-preserving and deduped. This is the single
    source of truth for what an agent may invoke — the agent loop uses it to
    decide which function schemas the model is offered, and the policy layer
    uses it to decide what may actually run."""
    # getattr rather than attribute access: agent stand-ins (tests, and any
    # duck-typed caller) may not carry every field, and a missing `tools` must
    # read as "granted nothing" rather than raising past the check.
    allowed = list(getattr(agent, "tools", None) or [])
    perms = getattr(agent, "permissions", None)
    tools_perm = perms.get("tools") if isinstance(perms, dict) else None
    if isinstance(tools_perm, dict):
        allowed += list(tools_perm.get("allow") or [])
    return list(dict.fromkeys(allowed))


def evaluate_tool_grant(agent: AgentConfig | None, action: str) -> PolicyDecision:
    """Deny any action the agent was not explicitly granted.

    The grant list used to gate only *which function schemas the model was
    offered* — a menu, not a boundary. Anything that named an action directly
    (a workflow node, a recipe, a scheduled job) reached the handler regardless,
    so an agent with `tools: []` could write files through a workflow while the
    model-facing tool list showed nothing. This is the boundary; every
    execution path checks it, and `dispatch_action` re-checks as a floor so a
    future caller can't reintroduce the bypass.
    """
    if agent is None:
        return PolicyDecision(
            "deny", f"No agent configured to grant {action!r}.", "tools.grant")
    if action in granted_actions(agent):
        return PolicyDecision("allow")
    return PolicyDecision(
        "deny",
        f"Action {action!r} is not granted to agent {agent.id!r}. "
        f"Add it under `tools:` in agents/{agent.id}.yaml to allow it.",
        "tools.grant",
    )


def evaluate_workflow_step(
    step: WorkflowStep,
    agent: AgentConfig | None = None,
    default_mode: str = "ask",
) -> PolicyDecision:
    if step.approval == "required":
        return PolicyDecision("ask", f"Step {step.id} requires approval.", "workflow.step.approval")
    if agent is None:
        if step.optional:
            return PolicyDecision("allow", f"Optional agent {step.agent} is not configured.")
        return PolicyDecision("deny", f"Agent {step.agent} is not configured.", "agent.available")
    mode = resolve_permission_mode(agent, default_mode)
    # plan_only and locked_down agents cannot execute any workflow step.
    # `ask` and `accept_edits` modes rely on the per-step `approval: required`
    # flag and on per-action evaluators (filesystem/shell/network) instead of
    # gating every step uniformly.
    if mode in NON_EXECUTING_MODES:
        return PolicyDecision(
            "deny",
            f"Agent {agent.id} permission_mode={mode}; step cannot execute.",
            f"permission_mode.{mode}",
        )
    return PolicyDecision("allow")


def _network_target_allowed(target: str, allow: Any) -> bool:
    """True if `target` matches an entry in a network `allow` list. Matches on
    exact host/URL or a path-boundary prefix (so `https://api.telegram.org`
    permits `.../botX/sendMessage` but NOT `api.telegram.org.evil.com`)."""
    if not isinstance(allow, list):
        return False
    t = str(target).strip().rstrip("/")
    for entry in allow:
        e = str(entry).strip().rstrip("/")
        if e and (e in {"*", "all"} or t == e or t.startswith(e + "/")):
            return True
    return False


def evaluate_network(agent: AgentConfig, target: str | None = None) -> PolicyDecision:
    perm = agent.permissions.get("network")
    mode = _mode(perm, default="deny")
    if mode == "allow":
        return PolicyDecision("allow")
    # Per-target egress allowlist: a specific declared target may be permitted
    # even when the default mode is ask/deny, so a channel capability's host
    # (e.g. api.telegram.org) gets through without opening the agent to all
    # network egress. (Milestone-E egress allowlist, in miniature.)
    if target and isinstance(perm, dict) and _network_target_allowed(target, perm.get("allow")):
        return PolicyDecision("allow")
    if mode == "ask":
        return PolicyDecision("ask", f"Network access requires approval for {target or 'target'}.", "network")
    return PolicyDecision("deny", "Network access is denied for this agent.", "network")


def evaluate_filesystem(agent: AgentConfig, path: str | Path, operation: str = "read") -> PolicyDecision:
    fs = agent.permissions.get("filesystem") if isinstance(agent.permissions, dict) else {}
    allow = list(fs.get("allow", [])) if isinstance(fs, dict) else []
    deny = list(fs.get("deny", [])) if isinstance(fs, dict) else []
    raw = str(Path(path).expanduser())
    if _matches_any(raw, deny):
        return PolicyDecision("deny", f"Filesystem {operation} denied by deny rule: {raw}", f"filesystem.{operation}")
    if allow and _matches_any(raw, allow):
        return PolicyDecision("allow")
    if allow:
        return PolicyDecision("ask", f"Filesystem {operation} outside allow list: {raw}", f"filesystem.{operation}")
    return PolicyDecision("deny", f"Filesystem {operation} has no allow list for this agent.", f"filesystem.{operation}")


def evaluate_resource_permission(agent: AgentConfig, resource: str, required: str) -> PolicyDecision:
    """Generic evaluator for flat scalar permissions like calendar/email/notifications/secrets.

    Agents declare grants like `calendar: read`, `notifications: send`, or
    `secrets: {allow: [TELEGRAM_BOT_TOKEN]}`; capabilities declare needs like
    `permissions: {calendar: "read"}` or `permissions: {secrets: {required: [...]}}`.

    Match rules:
      - missing/None → deny
      - string equal to required → allow
      - string "*" or "all" → allow (broad grant)
      - dict with `mode` field → respect allow/ask/deny
      - dict with `allow` list containing `required` → allow
      - dict with `allow` list not containing `required` → deny
    """
    permissions = agent.permissions or {}
    granted = permissions.get(resource)
    permission_tag = f"{resource}.{required}"
    if granted is None:
        return PolicyDecision(
            "deny",
            f"Agent {agent.id} does not grant {resource} permission.",
            permission_tag,
        )
    if isinstance(granted, str):
        if granted == required or granted in {"*", "all"}:
            return PolicyDecision("allow")
        return PolicyDecision(
            "deny",
            f"Agent {agent.id} grants {resource}={granted!r}, capability needs {required!r}.",
            permission_tag,
        )
    if isinstance(granted, dict):
        if "mode" in granted:
            mode = str(granted.get("mode", "deny"))
            if mode in {"allow", "*"}:
                return PolicyDecision("allow")
            if mode == "ask":
                return PolicyDecision(
                    "ask",
                    f"Agent {agent.id} {resource} requires approval for {required}.",
                    permission_tag,
                )
            return PolicyDecision(
                "deny",
                f"Agent {agent.id} {resource}.mode={mode}.",
                permission_tag,
            )
        if "allow" in granted:
            allowed = list(granted.get("allow") or [])
            if required in allowed or "*" in allowed or "all" in allowed:
                return PolicyDecision("allow")
            return PolicyDecision(
                "deny",
                f"Agent {agent.id} {resource}.allow={allowed!r} does not include {required!r}.",
                permission_tag,
            )
    return PolicyDecision(
        "deny",
        f"Agent {agent.id} has unsupported {resource} permission shape: {type(granted).__name__}.",
        permission_tag,
    )


def evaluate_shell(agent: AgentConfig, command: str) -> PolicyDecision:
    shell = agent.permissions.get("shell") if isinstance(agent.permissions, dict) else {}
    mode = _mode(shell, default="deny")
    if is_dangerous_command(command):
        return PolicyDecision("deny", "Command matches a dangerous shell pattern.", "shell")
    if mode == "allow":
        return PolicyDecision("allow")
    if mode == "restricted":
        allowed = list(shell.get("allow", [])) if isinstance(shell, dict) else []
        if allowed and any(fnmatch.fnmatch(command, pattern) for pattern in allowed):
            return PolicyDecision("allow")
        return PolicyDecision("ask", "Shell command is outside the restricted allow list.", "shell")
    if mode == "ask":
        return PolicyDecision("ask", "Shell command requires approval.", "shell")
    return PolicyDecision("deny", "Shell access is denied for this agent.", "shell")

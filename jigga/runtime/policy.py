from __future__ import annotations

import fnmatch
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


def _mode(config: dict[str, Any] | None, default: str = "deny") -> str:
    if not isinstance(config, dict):
        return default
    value = config.get("mode", default)
    return str(value or default)


def _matches_any(value: str, patterns: list[str]) -> bool:
    raw = str(Path(value).expanduser())
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


def evaluate_network(agent: AgentConfig, target: str | None = None) -> PolicyDecision:
    mode = _mode(agent.permissions.get("network"), default="deny")
    if mode == "allow":
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
    """Generic evaluator for flat scalar permissions like calendar/email/notifications/memory.

    Agents declare grants like `calendar: read` or `notifications: send`;
    capabilities declare needs like `permissions: {calendar: "read"}`.

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
    lowered = command.lower()
    if any(fnmatch.fnmatch(lowered, pattern) or pattern in lowered for pattern in DANGEROUS_SHELL_PATTERNS):
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

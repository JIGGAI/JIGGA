from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jigga.core.models import AgentConfig, WorkflowStep

PolicyStatus = Literal["allow", "deny", "ask"]


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
    normalized = str(Path(value).expanduser())
    for pattern in patterns:
        expanded = str(Path(pattern).expanduser())
        if fnmatch.fnmatch(normalized, expanded):
            return True
        if not any(token in expanded for token in "*?[") and (normalized == expanded or normalized.startswith(expanded.rstrip("/") + "/")):
            return True
    return False


def evaluate_workflow_step(step: WorkflowStep, agent: AgentConfig | None = None) -> PolicyDecision:
    if step.approval == "required":
        return PolicyDecision("ask", f"Step {step.id} requires approval.", "workflow.step.approval")
    if agent is None:
        if step.optional:
            return PolicyDecision("allow", f"Optional agent {step.agent} is not configured.")
        return PolicyDecision("deny", f"Agent {step.agent} is not configured.", "agent.available")
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

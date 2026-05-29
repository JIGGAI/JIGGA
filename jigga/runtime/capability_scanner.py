"""Static security scanner for capability manifests.

Surfaces risk findings before a user-local capability is approved. The scanner
is intentionally conservative: it errs on the side of flagging suspicious
patterns. Findings are advisory — the user can still approve a flagged pack
via `jigga capabilities approve --approve` after reviewing.

Spec: docs/tools/SKILL_SECURITY_SCANNER.md
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from jigga.runtime.capabilities import CapabilityManifest

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}

# Path patterns the spec calls out as sensitive. Matched as substrings
# (case-insensitive) inside any declared filesystem path.
SENSITIVE_PATH_TOKENS = (
    ".ssh",
    ".gnupg",
    ".aws",
    "keychain",
    "wallet",
    "id_rsa",
    "id_ed25519",
    ".env",
    "secrets",
    "credentials",
)

# Patterns that grant essentially unrestricted filesystem access.
BROAD_FS_PATTERNS = ("/", "/**", "~", "~/", "~/**", "**", "*")

# Handler paths that allow arbitrary code execution. Approving a pack that
# imports these is effectively giving it shell-equivalent access.
SUSPICIOUS_HANDLER_PREFIXES = (
    "os:",
    "subprocess:",
    "shutil:",
    "ctypes:",
    "importlib:",
    "builtins:",
)

# Remote-script install patterns. The spec specifically calls out
# `curl | sh` / `wget | sh` style installers as a reject signal.
REMOTE_SCRIPT_PATTERNS = (
    re.compile(r"curl[^\n]*\|\s*(sh|bash|zsh)\b"),
    re.compile(r"wget[^\n]*\|\s*(sh|bash|zsh)\b"),
    re.compile(r"curl[^\n]*-fsSL[^\n]*\|\s*\w*sh"),
)


@dataclass(frozen=True)
class RiskFinding:
    severity: str
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class CapabilityRiskReport:
    capability: str
    findings: list[RiskFinding] = field(default_factory=list)

    @property
    def risk(self) -> str:
        if not self.findings:
            return "low"
        return max(self.findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 0)).severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "risk": self.risk,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def _check_filesystem(permissions: dict[str, Any]) -> list[RiskFinding]:
    findings: list[RiskFinding] = []
    filesystem = permissions.get("filesystem")
    if not isinstance(filesystem, dict):
        return findings
    for operation in ("read", "write", "deny"):
        paths = filesystem.get(operation) or []
        for path in paths:
            text = str(path).strip()
            if text in BROAD_FS_PATTERNS:
                findings.append(
                    RiskFinding(
                        severity="high",
                        code="broad_filesystem_access",
                        detail=f"filesystem.{operation} entry {text!r} grants unrestricted access.",
                    )
                )
            lower = text.lower()
            for token in SENSITIVE_PATH_TOKENS:
                if token in lower:
                    findings.append(
                        RiskFinding(
                            severity="high",
                            code="sensitive_path_access",
                            detail=f"filesystem.{operation} entry {text!r} touches sensitive path token {token!r}.",
                        )
                    )
                    break
    return findings


def _check_resources(permissions: dict[str, Any]) -> list[RiskFinding]:
    findings: list[RiskFinding] = []
    if permissions.get("shell") is not None:
        findings.append(
            RiskFinding(
                severity="medium",
                code="shell_access_requested",
                detail="Manifest declares shell access.",
            )
        )
    if permissions.get("network") is not None:
        findings.append(
            RiskFinding(
                severity="medium",
                code="network_access_requested",
                detail="Manifest declares network access.",
            )
        )
    if permissions.get("secrets") is not None:
        findings.append(
            RiskFinding(
                severity="high",
                code="secrets_access_requested",
                detail="Manifest declares secrets access.",
            )
        )
    return findings


def _check_handler(handler: str) -> list[RiskFinding]:
    if not handler:
        return []
    for prefix in SUSPICIOUS_HANDLER_PREFIXES:
        if handler.startswith(prefix):
            return [
                RiskFinding(
                    severity="high",
                    code="suspicious_handler",
                    detail=f"Handler {handler!r} resolves to a module that allows arbitrary code execution.",
                )
            ]
    return []


def _check_pack_files(pack_dir: Path) -> list[RiskFinding]:
    """Scan files inside the capability pack for remote-script install patterns
    and post-install hook indicators. Intentionally only walks the pack's own
    directory, not symlinked paths (manifest loader already rejects those)."""
    findings: list[RiskFinding] = []
    if not pack_dir.exists():
        return findings
    for file in pack_dir.rglob("*"):
        if not file.is_file() or file.is_symlink():
            continue
        if file.suffix.lower() in {".sh", ".bash", ".zsh", ".py", ".js", ".ts", ".md", ".yaml", ".yml"}:
            try:
                content = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pattern in REMOTE_SCRIPT_PATTERNS:
                if pattern.search(content):
                    findings.append(
                        RiskFinding(
                            severity="high",
                            code="remote_script_install",
                            detail=f"{file.relative_to(pack_dir)} contains a remote-script install pattern.",
                        )
                    )
                    break
        # Post-install hook names — common conventions
        if file.name in {"install.sh", "postinstall.sh", "setup.sh"}:
            findings.append(
                RiskFinding(
                    severity="medium",
                    code="post_install_hook",
                    detail=f"Pack contains {file.name}, which capability runtimes do not invoke automatically; flag for review.",
                )
            )
    return findings


def scan_capability(
    capability: CapabilityManifest,
    pack_dir: Path | None = None,
) -> CapabilityRiskReport:
    """Produce a risk report for a capability manifest.

    `pack_dir` is the directory containing the manifest. When omitted, only
    the manifest itself is scanned (no script content inspection).
    """
    findings: list[RiskFinding] = []
    permissions = capability.permissions if isinstance(capability.permissions, dict) else {}
    findings.extend(_check_filesystem(permissions))
    findings.extend(_check_resources(permissions))
    findings.extend(_check_handler(capability.handler))
    if pack_dir is not None:
        findings.extend(_check_pack_files(pack_dir))
    return CapabilityRiskReport(capability=capability.name, findings=findings)

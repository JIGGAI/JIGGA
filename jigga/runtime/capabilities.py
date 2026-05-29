from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, list_config_files, read_json, read_yaml, write_json

APPROVALS_FILE = "capability_approvals.json"

VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_CAPABILITY_TYPES = {"native", "skill_pack", "mcp_server"}

# Auto-assigned handler key for each capability type when the manifest does
# not declare one explicitly. Built-in dispatch table registers each of these.
DEFAULT_HANDLERS_BY_TYPE = {
    "native": "dry_run.generic",
    "skill_pack": "skill_pack.default",
    "mcp_server": "mcp_server.subprocess",
}


@dataclass(frozen=True)
class CapabilityManifest:
    name: str
    version: str
    summary: str
    actions: list[str]
    type: str = "native"
    triggers: list[str] = field(default_factory=list)
    requires: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"
    handler: str = "dry_run.generic"
    # mcp_server-specific fields. Ignored for other types.
    command: str | None = None
    args: list[str] = field(default_factory=list)
    transport: str = "stdio"
    # skill_pack-specific field: filename inside the pack dir holding the
    # instructions/system-prompt the model is given on dispatch.
    instructions: str = "instructions.md"
    source: str | None = None
    manifest_hash: str | None = None
    bundled: bool = False

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        source: str | None = None,
        bundled: bool = False,
        manifest_hash: str | None = None,
    ) -> "CapabilityManifest":
        missing = [key for key in ("name", "version", "summary", "actions") if not data.get(key)]
        if missing:
            raise ValueError(f"Capability manifest missing required fields: {', '.join(missing)}")
        actions = data.get("actions")
        if not isinstance(actions, list) or not actions or not all(isinstance(item, str) and item for item in actions):
            raise ValueError("Capability manifest field 'actions' must be a non-empty list of strings")
        risk = str(data.get("risk_level", "low"))
        if risk not in VALID_RISK_LEVELS:
            raise ValueError(f"Invalid capability risk_level: {risk!r}")
        kind = str(data.get("type", "native"))
        if kind not in VALID_CAPABILITY_TYPES:
            raise ValueError(
                f"Invalid capability type: {kind!r}. "
                f"Allowed: {', '.join(sorted(VALID_CAPABILITY_TYPES))}."
            )

        # Per-type required-field validation. Keep loose where the doc is loose
        # — skill_pack's `instructions` defaults to `instructions.md` and the
        # handler reports a clear error if the file is missing at dispatch.
        command = data.get("command")
        if kind == "mcp_server":
            if not command or not isinstance(command, str):
                raise ValueError("mcp_server capability requires a non-empty 'command' string")

        args = data.get("args") or []
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("Capability 'args' must be a list of strings")

        # Handler defaults are type-aware. If the manifest declares a handler
        # explicitly, that wins (so native packs can still ship custom code via
        # dotted import paths). Otherwise the type picks a sensible default.
        handler = data.get("handler")
        if not handler:
            handler = DEFAULT_HANDLERS_BY_TYPE[kind]

        return cls(
            name=str(data["name"]),
            version=str(data["version"]),
            summary=str(data["summary"]),
            actions=list(actions),
            type=kind,
            triggers=list(data.get("triggers") or []),
            requires=dict(data.get("requires") or {}),
            permissions=dict(data.get("permissions") or {}),
            risk_level=risk,
            handler=str(handler),
            command=str(command) if command else None,
            args=[str(item) for item in args],
            transport=str(data.get("transport", "stdio")),
            instructions=str(data.get("instructions", "instructions.md")),
            source=source,
            manifest_hash=manifest_hash,
            bundled=bundled,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "actions": self.actions,
            "type": self.type,
            "triggers": self.triggers,
            "requires": self.requires,
            "permissions": self.permissions,
            "risk_level": self.risk_level,
            "handler": self.handler,
            "command": self.command,
            "args": self.args,
            "transport": self.transport,
            "instructions": self.instructions,
            "source": self.source,
            "manifest_hash": self.manifest_hash,
            "bundled": self.bundled,
        }


BUILTIN_CAPABILITY_DATA: list[dict[str, Any]] = [
    {
        "name": "calendar",
        "version": "0.1.0",
        "summary": "Dry-run calendar inspection actions for MVP workflows.",
        "actions": ["calendar.list_events", "calendar.get_event"],
        "permissions": {"calendar": "read"},
        "risk_level": "low",
        "handler": "dry_run.calendar",
    },
    {
        "name": "email",
        "version": "0.1.0",
        "summary": "Dry-run email search actions for MVP workflows.",
        "actions": ["email.search"],
        "permissions": {"email": "read"},
        "risk_level": "low",
        "handler": "dry_run.email",
    },
    {
        "name": "notifications",
        "version": "0.2.0",
        "summary": "Cross-platform desktop notifications (notify-send on Linux, osascript on macOS).",
        "actions": ["notifications.send"],
        "permissions": {"notifications": "send"},
        "risk_level": "low",
        "handler": "runtime.notifications",
    },
    {
        "name": "summarization",
        "version": "0.1.0",
        "summary": "MVP text/context summarization actions.",
        "actions": ["summarize_day", "summarize_relevant_context"],
        "permissions": {"memory": "read"},
        "risk_level": "low",
        "handler": "dry_run.summarization",
    },

    {
        "name": "subagent-delegation",
        "version": "0.1.0",
        "summary": "Controlled subagent spawning through policy-checked runtime adapters.",
        "actions": ["spawn_subagent"],
        "permissions": {"delegation": "spawn_subagent"},
        "risk_level": "medium",
        "handler": "runtime.spawn_subagent",
    },
    {
        "name": "content-drafting",
        "version": "0.1.0",
        "summary": "Dry-run content strategy and drafting actions for demo workflows.",
        "actions": [
            "extract_core_message",
            "draft_linkedin_post",
            "draft_thread",
            "draft_blurb",
            "review_tone_and_claims",
            "prepare_distribution_package",
        ],
        # Paths are absolute so they match the content_strategist agent's
        # filesystem allow list (`~/Projects/content`, `~/Projects/content/drafts`)
        # without forcing every user to override either side. Capability paths
        # are evaluated against the executing agent's filesystem policy.
        "permissions": {
            "filesystem": {
                "read": ["~/Projects/content/**"],
                "write": ["~/Projects/content/drafts/**"],
            }
        },
        "risk_level": "low",
        "handler": "dry_run.generic",
    },
]


def bundled_capabilities() -> list[CapabilityManifest]:
    return [CapabilityManifest.from_dict(item, source="builtin", bundled=True) for item in BUILTIN_CAPABILITY_DATA]


def _manifest_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reject_symlinked_manifest(path: Path) -> None:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError(f"Capability manifest cannot be a symlink or live under a symlinked directory: {path}")


def load_capability_manifest(path: Path) -> CapabilityManifest:
    _reject_symlinked_manifest(path)
    return CapabilityManifest.from_dict(
        read_yaml(path),
        source=str(path),
        bundled=False,
        manifest_hash=_manifest_hash(path),
    )


def scan_capability_dir(path: Path) -> list[CapabilityManifest]:
    if not path.exists():
        return []
    manifests: list[CapabilityManifest] = []
    for file in list_config_files(path):
        if file.name == "manifest.yaml" or file.name == "manifest.yml":
            manifests.append(load_capability_manifest(file))
    return manifests


def approvals_path(approvals_dir: Path) -> Path:
    return approvals_dir / APPROVALS_FILE


def load_approval_index(approvals_dir: Path) -> dict[str, dict[str, Any]]:
    """Read the recorded capability approvals as `{name: {manifest_hash, approved_at, ...}}`.

    Bundled capabilities are not subject to approval; this index covers only
    user-local packs. Returns an empty dict if no approvals file exists yet.
    """
    path = approvals_path(approvals_dir)
    if not path.exists():
        return {}
    raw = read_json(path)
    return dict(raw.get("approvals") or {})


def record_approval(approvals_dir: Path, capability: CapabilityManifest) -> dict[str, Any]:
    """Record an approval for a user-local capability pack.

    Keyed by capability name; the recorded manifest_hash is what gets compared
    on subsequent loads. Hash mismatch (manifest changed since approval) means
    the capability falls back to pending until re-approved.
    """
    if capability.bundled:
        raise ValueError("Bundled capabilities do not require approval")
    if capability.manifest_hash is None:
        raise ValueError("Cannot record approval for capability without a manifest_hash")
    ensure_dir(approvals_dir)
    path = approvals_path(approvals_dir)
    current = read_json(path) if path.exists() else {"version": 1, "approvals": {}}
    entry = {
        "manifest_hash": capability.manifest_hash,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "source": capability.source,
    }
    current.setdefault("approvals", {})[capability.name] = entry
    write_json(path, current)
    return entry


class CapabilityRegistry:
    def __init__(
        self,
        capabilities: list[CapabilityManifest],
        pending: list[CapabilityManifest] | None = None,
    ):
        self.capabilities = capabilities
        self.pending = pending or []
        self._by_name = {capability.name: capability for capability in capabilities}
        self._by_action: dict[str, CapabilityManifest] = {}
        for capability in capabilities:
            for action in capability.actions:
                # Earlier capabilities win. Callers pass highest precedence first.
                self._by_action.setdefault(action, capability)

    @classmethod
    def load(
        cls,
        user_capabilities: Path | None = None,
        project_capabilities: Path | None = None,
        approvals_dir: Path | None = None,
    ) -> "CapabilityRegistry":
        """Load capability manifests.

        Precedence: project > user > bundled. When `approvals_dir` is given,
        user-local manifests must have a recorded approval whose manifest_hash
        matches the file on disk; unapproved or hash-mismatch entries land in
        `pending` and are not dispatched. Bundled capabilities are never gated
        by approval. Callers that don't pass `approvals_dir` keep the old
        behaviour (no gating) — this is the safe MVP default for tests and
        ad-hoc loads.
        """
        approved = load_approval_index(approvals_dir) if approvals_dir is not None else None
        active: list[CapabilityManifest] = []
        pending: list[CapabilityManifest] = []
        if project_capabilities is not None:
            for capability in scan_capability_dir(project_capabilities):
                _route_user_capability(capability, approved, active, pending)
        if user_capabilities is not None:
            for capability in scan_capability_dir(user_capabilities):
                _route_user_capability(capability, approved, active, pending)
        active.extend(bundled_capabilities())
        return cls(active, pending)

    def list(self) -> list[CapabilityManifest]:
        return list(self.capabilities)

    def get(self, name: str) -> CapabilityManifest | None:
        return self._by_name.get(name)

    def resolve_action(self, action: str) -> CapabilityManifest | None:
        return self._by_action.get(action)

    def list_pending(self) -> list[CapabilityManifest]:
        return list(self.pending)

    def to_index(self) -> dict[str, Any]:
        return {
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "pending": [capability.to_dict() for capability in self.pending],
            "actions": {
                action: capability.name
                for action, capability in sorted(self._by_action.items())
            },
        }


def _route_user_capability(
    capability: CapabilityManifest,
    approved: dict[str, dict[str, Any]] | None,
    active: list[CapabilityManifest],
    pending: list[CapabilityManifest],
) -> None:
    if approved is None:
        active.append(capability)
        return
    recorded = approved.get(capability.name)
    if recorded is not None and recorded.get("manifest_hash") == capability.manifest_hash:
        active.append(capability)
    else:
        pending.append(capability)

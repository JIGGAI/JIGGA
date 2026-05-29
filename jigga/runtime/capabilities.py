from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jigga.core.io import list_config_files, read_yaml

VALID_RISK_LEVELS = {"low", "medium", "high"}


@dataclass(frozen=True)
class CapabilityManifest:
    name: str
    version: str
    summary: str
    actions: list[str]
    triggers: list[str] = field(default_factory=list)
    requires: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"
    source: str | None = None
    bundled: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str | None = None, bundled: bool = False) -> "CapabilityManifest":
        missing = [key for key in ("name", "version", "summary", "actions") if not data.get(key)]
        if missing:
            raise ValueError(f"Capability manifest missing required fields: {', '.join(missing)}")
        actions = data.get("actions")
        if not isinstance(actions, list) or not actions or not all(isinstance(item, str) and item for item in actions):
            raise ValueError("Capability manifest field 'actions' must be a non-empty list of strings")
        risk = str(data.get("risk_level", "low"))
        if risk not in VALID_RISK_LEVELS:
            raise ValueError(f"Invalid capability risk_level: {risk!r}")
        return cls(
            name=str(data["name"]),
            version=str(data["version"]),
            summary=str(data["summary"]),
            actions=list(actions),
            triggers=list(data.get("triggers") or []),
            requires=dict(data.get("requires") or {}),
            permissions=dict(data.get("permissions") or {}),
            risk_level=risk,
            source=source,
            bundled=bundled,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "actions": self.actions,
            "triggers": self.triggers,
            "requires": self.requires,
            "permissions": self.permissions,
            "risk_level": self.risk_level,
            "source": self.source,
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
    },
    {
        "name": "email",
        "version": "0.1.0",
        "summary": "Dry-run email search actions for MVP workflows.",
        "actions": ["email.search"],
        "permissions": {"email": "read"},
        "risk_level": "low",
    },
    {
        "name": "notifications",
        "version": "0.1.0",
        "summary": "Dry-run notification delivery actions for MVP workflows.",
        "actions": ["notifications.send"],
        "permissions": {"notifications": "send"},
        "risk_level": "low",
    },
    {
        "name": "summarization",
        "version": "0.1.0",
        "summary": "MVP text/context summarization actions.",
        "actions": ["summarize_day", "summarize_relevant_context"],
        "permissions": {"memory": "read"},
        "risk_level": "low",
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
        "permissions": {"filesystem": {"read": ["./content/**"], "write": ["./drafts/**"]}},
        "risk_level": "medium",
    },
]


def bundled_capabilities() -> list[CapabilityManifest]:
    return [CapabilityManifest.from_dict(item, source="builtin", bundled=True) for item in BUILTIN_CAPABILITY_DATA]


def load_capability_manifest(path: Path) -> CapabilityManifest:
    return CapabilityManifest.from_dict(read_yaml(path), source=str(path), bundled=False)


def scan_capability_dir(path: Path) -> list[CapabilityManifest]:
    if not path.exists():
        return []
    manifests: list[CapabilityManifest] = []
    for file in list_config_files(path):
        if file.name == "manifest.yaml" or file.name == "manifest.yml":
            manifests.append(load_capability_manifest(file))
    return manifests


class CapabilityRegistry:
    def __init__(self, capabilities: list[CapabilityManifest]):
        self.capabilities = capabilities
        self._by_name = {capability.name: capability for capability in capabilities}
        self._by_action: dict[str, CapabilityManifest] = {}
        for capability in capabilities:
            for action in capability.actions:
                # Earlier capabilities win. Callers pass highest precedence first.
                self._by_action.setdefault(action, capability)

    @classmethod
    def load(cls, user_capabilities: Path | None = None, project_capabilities: Path | None = None) -> "CapabilityRegistry":
        capabilities: list[CapabilityManifest] = []
        if project_capabilities is not None:
            capabilities.extend(scan_capability_dir(project_capabilities))
        if user_capabilities is not None:
            capabilities.extend(scan_capability_dir(user_capabilities))
        capabilities.extend(bundled_capabilities())
        return cls(capabilities)

    def list(self) -> list[CapabilityManifest]:
        return list(self.capabilities)

    def get(self, name: str) -> CapabilityManifest | None:
        return self._by_name.get(name)

    def resolve_action(self, action: str) -> CapabilityManifest | None:
        return self._by_action.get(action)

    def to_index(self) -> dict[str, Any]:
        return {
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "actions": {
                action: capability.name
                for action, capability in sorted(self._by_action.items())
            },
        }

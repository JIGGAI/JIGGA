from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, get_args, Literal, TypeVar


TaskState = Literal["pending", "claimed", "running", "blocked", "needs_approval", "failed", "completed", "archived"]
PermissionMode = Literal["plan_only", "ask", "accept_edits", "autonomous", "locked_down"]
T = TypeVar("T")


def validate_permission_mode(mode: str) -> str:
    if mode not in get_args(PermissionMode):
        raise ValueError(
            f"Invalid permission_mode: {mode!r}. "
            f"Allowed: {', '.join(get_args(PermissionMode))}."
        )
    return mode


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _filter(cls: type[T], data: dict[str, Any]) -> dict[str, Any]:
    names = {item.name for item in fields(cls)}
    return {key: value for key, value in data.items() if key in names}


@dataclass
class AgentConfig:
    id: str
    name: str
    role: str
    description: str | None = None
    model: str | None = None
    memory_scope: str | None = None
    permission_mode: str | None = None
    tools: list[str] = field(default_factory=list)
    wake: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    workflows: list[str] = field(default_factory=list)
    delegation: dict[str, Any] = field(default_factory=dict)
    # The default/primary agent (chief of staff / personal assistant) — the
    # catch-all for unrouted inbound and the human's direct assistant. At most
    # one agent sets this; resolve_default_agent picks it.
    default: bool = False
    source: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str | None = None) -> "AgentConfig":
        obj = cls(**_filter(cls, data))
        if obj.permission_mode is not None:
            validate_permission_mode(obj.permission_mode)
        obj.source = source
        return obj

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TeamConfig:
    id: str
    name: str
    purpose: str | None = None
    memory_scope: str | None = None
    agents: list[dict[str, Any]] = field(default_factory=list)
    default_workflows: list[str] = field(default_factory=list)
    policies: dict[str, Any] = field(default_factory=dict)
    routing: dict[str, Any] = field(default_factory=dict)
    source: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str | None = None) -> "TeamConfig":
        obj = cls(**_filter(cls, data))
        obj.source = source
        return obj

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowStep:
    id: str
    action: str
    agent: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    output: str | None = None
    approval: str | None = None
    optional: bool = False
    on_fail: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowStep":
        return cls(**_filter(cls, data))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowConfig:
    id: str
    name: str
    purpose: str | None = None
    status: str = "draft"
    trigger: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)
    steps: list[WorkflowStep] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    source: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str | None = None) -> "WorkflowConfig":
        prepared = dict(data)
        prepared["steps"] = [WorkflowStep.from_dict(step) for step in prepared.get("steps", [])]
        obj = cls(**_filter(cls, prepared))
        obj.source = source
        return obj

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MemoryScope:
    id: str
    name: str | None = None
    description: str | None = None
    includes: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    retrieval: dict[str, Any] = field(default_factory=dict)
    sensitivity: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, scope_id: str, data: dict[str, Any]) -> "MemoryScope":
        return cls(id=scope_id, **_filter(cls, data))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Task:
    id: str
    title: str
    description: str | None = None
    assignee: str | None = None
    workflow_id: str | None = None
    state: str = "pending"
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(**_filter(cls, data))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class State:
    version: int = 1
    initialized_at: str = field(default_factory=now_iso)
    last_supervisor_tick_at: str | None = None
    agents: dict[str, dict[str, Any]] = field(default_factory=dict)
    teams: dict[str, dict[str, Any]] = field(default_factory=dict)
    workflows: dict[str, dict[str, Any]] = field(default_factory=dict)
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "State":
        return cls(**_filter(cls, data))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_task_state(state: str) -> str:
    if state not in get_args(TaskState):
        raise ValueError(f"Invalid task state: {state}")
    return state

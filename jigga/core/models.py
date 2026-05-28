from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, get_args, Literal, TypeVar


TaskState = Literal["pending", "claimed", "running", "blocked", "needs_approval", "failed", "completed", "archived"]
T = TypeVar("T")


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
    tools: list[str] = field(default_factory=list)
    wake: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    workflows: list[str] = field(default_factory=list)
    source: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str | None = None) -> "AgentConfig":
        obj = cls(**_filter(cls, data))
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

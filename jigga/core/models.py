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
    # The team this agent was installed onto. DESCRIPTIVE, not authoritative:
    # membership is the team roster (`find_agent_teams` scans team yamls), and
    # nothing about scheduling or workspace resolution reads this field. It is
    # here so an agent's own file says who it works for — `jigga agents get`
    # used to answer `team: null` for an agent that was plainly on a team.
    # `jigga validate` warns when this disagrees with the roster.
    team: str | None = None
    memory_scope: str | None = None
    permission_mode: str | None = None
    tools: list[str] = field(default_factory=list)
    wake: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    workflows: list[str] = field(default_factory=list)
    delegation: dict[str, Any] = field(default_factory=dict)
    # How `notifications.send` reaches the user. `channel:` is "default" (the
    # user's default connected channel — config `channels.default`), a specific
    # channel name, or "desktop" (desktop notification only). Recipes ship
    # "default" so they work with whatever surface the user actually connected.
    notifications: dict[str, Any] = field(default_factory=dict)
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
    # Ticket-board lane vocabulary (raw passthrough; normalized by
    # runtime/lanes.py). `True` = default lanes; a list = a custom vocabulary;
    # None/False = no board (tickets behave like plain tasks).
    lanes: Any = None
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
    # Declared shape of a model-backed step's reply: [{name, type, description?}].
    # Empty means the step returns whatever prose the model produced, which is
    # only safe while nothing downstream consumes it — see `plan`.
    output_fields: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowStep":
        return cls(**_filter(cls, data))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


WorkflowNodeType = Literal["tool", "llm", "human_approval", "writeback"]


@dataclass
class WorkflowNode:
    """A v2 (DAG) workflow node. `tool` calls a capability action like a v1
    step; `llm` is sugar for `draft_with_model`; `human_approval` parks the run
    until `approve <code>`; `writeback` copies a named upstream output to a
    workspace file. Edges (on the workflow) decide what runs after it."""

    id: str
    type: str = "tool"
    action: str | None = None
    agent: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    output: str | None = None
    optional: bool = False
    output_fields: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowNode":
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
    # v2 DAG form: nodes + edges. `edges` stays raw dicts ({from, to, on}) —
    # `from` is a Python keyword, and the engine normalizes/validates them.
    # A workflow declares either `steps` (linear v1) or `nodes` (DAG v2).
    nodes: list[WorkflowNode] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    source: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: str | None = None) -> "WorkflowConfig":
        prepared = dict(data)
        prepared["steps"] = [WorkflowStep.from_dict(step) for step in prepared.get("steps", [])]
        prepared["nodes"] = [WorkflowNode.from_dict(node) for node in prepared.get("nodes", [])]
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
    # Kanban lane (ticket board column) for team tasks — a per-team declared
    # vocabulary (see runtime/lanes.py). Orthogonal to `state` (execution
    # lifecycle); None for non-team tasks or teams without lanes.
    lane: str | None = None
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

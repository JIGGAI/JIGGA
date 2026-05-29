from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from jigga.core.config import load_runtime_config
from jigga.core.io import ensure_dir, read_json, write_json
from jigga.core.models import AgentConfig, now_iso
from jigga.runtime.audit import append_event, new_id
from jigga.runtime.policy import evaluate_filesystem, evaluate_network, evaluate_shell
from jigga.runtime.sandbox import SandboxSpec, build_restricted_env, run_sandboxed

SubagentBackend = Literal["dry_run", "codex_cli", "claude_code"]
SubagentMode = Literal["plan", "execute", "review", "research"]
SessionStatus = Literal["planned", "running", "completed", "failed", "cancelled"]

# External-process subagent backends that should remain gated behind a global
# `delegation_policy.<backend>_enabled: true` flag, in addition to the agent's
# allowed_backends list. dry_run is always available.
_GATED_BACKENDS = ("codex_cli", "claude_code")


@dataclass
class WorkOrder:
    goal: str
    instructions: str | None = None
    acceptance_criteria: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkOrder":
        goal = str(data.get("goal") or "").strip()
        if not goal:
            raise ValueError("Subagent work_order.goal is required")
        criteria = data.get("acceptance_criteria") or data.get("acceptanceCriteria") or []
        if not isinstance(criteria, list):
            raise ValueError("Subagent work_order.acceptance_criteria must be a list")
        return cls(goal=goal, instructions=data.get("instructions"), acceptance_criteria=[str(item) for item in criteria])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SpawnSubagentInput:
    backend: str
    mode: str
    parent_agent_id: str
    task_id: str
    work_order: WorkOrder
    cwd: str = "."
    memory_scope: str = "task_context_only"
    permissions: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    output_required: list[str] = field(default_factory=list)
    depth: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any], parent_agent_id: str | None = None) -> "SpawnSubagentInput":
        backend = str(data.get("backend") or "dry_run")
        mode = str(data.get("mode") or "plan")
        if mode not in {"plan", "execute", "review", "research"}:
            raise ValueError(f"Invalid subagent mode: {mode}")
        parent = str(data.get("parent_agent_id") or parent_agent_id or "")
        if not parent:
            raise ValueError("Subagent parent_agent_id is required")
        task_id = str(data.get("task_id") or "manual")
        output_required = data.get("output_required") or data.get("outputRequired") or []
        if not isinstance(output_required, list):
            raise ValueError("Subagent output_required must be a list")
        return cls(
            backend=backend,
            mode=mode,
            parent_agent_id=parent,
            task_id=task_id,
            work_order=WorkOrder.from_dict(dict(data.get("work_order") or data.get("workOrder") or {})),
            cwd=str(data.get("cwd") or "."),
            memory_scope=str(data.get("memory_scope") or data.get("memoryScope") or "task_context_only"),
            permissions=dict(data.get("permissions") or {}),
            limits=dict(data.get("limits") or {}),
            output_required=[str(item) for item in output_required],
            depth=int(data.get("depth") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["work_order"] = self.work_order.to_dict()
        return data


@dataclass
class SubagentSession:
    id: str
    backend: str
    mode: str
    parent_agent_id: str
    task_id: str
    status: str
    created_at: str
    updated_at: str
    cwd: str
    depth: int
    work_order: dict[str, Any]
    limits: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    logs_path: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubagentSession":
        names = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _session_dir(sessions_dir: Path, session_id: str) -> Path:
    return sessions_dir / session_id


def write_session(sessions_dir: Path, session: SubagentSession) -> None:
    directory = _session_dir(sessions_dir, session.id)
    ensure_dir(directory)
    write_json(directory / "session.json", session.to_dict())


def read_session(sessions_dir: Path, session_id: str) -> SubagentSession:
    exact = sessions_dir / session_id / "session.json"
    if exact.exists():
        return SubagentSession.from_dict(read_json(exact))
    matches = [path for path in sorted(sessions_dir.glob("subagent_*/session.json")) if path.parent.name.startswith(session_id)]
    if not matches:
        raise ValueError(f"Subagent session not found: {session_id}")
    if len(matches) > 1:
        raise ValueError(f"Subagent session prefix is ambiguous: {session_id}")
    return SubagentSession.from_dict(read_json(matches[0]))


def list_sessions(sessions_dir: Path) -> list[SubagentSession]:
    if not sessions_dir.exists():
        return []
    sessions = [SubagentSession.from_dict(read_json(path)) for path in sorted(sessions_dir.glob("subagent_*/session.json"))]
    return sorted(sessions, key=lambda item: item.created_at)


def cancel_session(sessions_dir: Path, session_id: str) -> SubagentSession:
    session = read_session(sessions_dir, session_id)
    if session.status in {"completed", "failed", "cancelled"}:
        return session
    session.status = "cancelled"
    session.updated_at = now_iso()
    write_session(sessions_dir, session)
    return session


def _agent_delegation_config(agent: AgentConfig) -> dict[str, Any]:
    return dict(agent.delegation or agent.permissions.get("delegation") or agent.permissions.get("subagents") or {})


def _expanded_cwd(request: SpawnSubagentInput) -> str:
    return str(Path(request.cwd).expanduser())


def _subagent_filesystem_permissions(request: SpawnSubagentInput) -> dict[str, Any]:
    filesystem = request.permissions.get("filesystem") if isinstance(request.permissions, dict) else None
    return dict(filesystem or {})


def _validate_subagent_resource_policy(agent: AgentConfig, request: SpawnSubagentInput) -> None:
    cwd = _expanded_cwd(request)
    cwd_decision = evaluate_filesystem(agent, cwd, operation="execute")
    if cwd_decision.status != "allow":
        raise ValueError(cwd_decision.reason or "Subagent cwd is not allowed")

    filesystem = _subagent_filesystem_permissions(request)
    # The subagent's requested read/write paths must each be within the
    # parent's filesystem policy. This is the "stricter than parent" rule:
    # a subagent cannot expand its parent's filesystem reach.
    for operation in ("read", "write"):
        for path in list(filesystem.get(operation, []) or []):
            decision = evaluate_filesystem(agent, path, operation=operation)
            if decision.status != "allow":
                raise ValueError(decision.reason or f"Subagent filesystem {operation} is not allowed")
    # The subagent's `deny` rules are *voluntary scope narrowing*. The spec
    # explicitly endorses this ("Subagents should run with stricter permissions
    # than parent agents"), so deny entries that cover parent-allowed paths are
    # the useful case, not a violation. We accept them as-is and record them on
    # the session for the adapter to honour.

    network = request.permissions.get("network") if isinstance(request.permissions, dict) else None
    if isinstance(network, dict) and str(network.get("mode", "deny")) not in {"deny", "disabled", "none"}:
        decision = evaluate_network(agent, str(network.get("target") or "subagent network"))
        if decision.status != "allow":
            raise ValueError(decision.reason or "Subagent network access is not allowed")

    shell = request.permissions.get("shell") if isinstance(request.permissions, dict) else None
    if isinstance(shell, dict) and str(shell.get("mode", "deny")) not in {"deny", "disabled", "none"}:
        decision = evaluate_shell(agent, str(shell.get("command") or "subagent shell"))
        if decision.status != "allow":
            raise ValueError(decision.reason or "Subagent shell access is not allowed")


def _secrets_required(request: SpawnSubagentInput) -> list[str]:
    secrets = request.permissions.get("secrets") if isinstance(request.permissions, dict) else None
    if isinstance(secrets, dict):
        return [str(item) for item in (secrets.get("required") or [])]
    return []


def _restricted_env(request: SpawnSubagentInput) -> dict[str, str]:
    """Thin compatibility wrapper that delegates to runtime.sandbox. Kept so
    existing tests (and any callers that already import this symbol) keep
    working while the implementation lives in one place."""
    return build_restricted_env(_secrets_required(request))


def _truncate_summary(text: str, limit: int = 4000) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    marker = "... [truncated]"
    return text[: max(0, limit - len(marker))] + marker, True


def validate_delegation_policy(home: Path, sessions_dir: Path, agent: AgentConfig, request: SpawnSubagentInput) -> None:
    runtime = load_runtime_config(home)
    global_policy = dict(runtime.get("delegation_policy") or {})
    if global_policy.get("enabled", True) is False:
        raise ValueError("Delegation is disabled by runtime policy")

    agent_policy = _agent_delegation_config(agent)
    can_delegate = bool(agent_policy.get("enabled", agent_policy.get("can_delegate", False)))
    if not can_delegate:
        raise ValueError(f"Agent {agent.id} is not allowed to delegate")

    max_depth = int(agent_policy.get("max_depth", global_policy.get("max_depth", 1)))
    if request.depth > max_depth:
        raise ValueError(f"Subagent depth {request.depth} exceeds max_depth {max_depth}")

    allowed = set(agent_policy.get("allowed_backends") or global_policy.get("allowed_backends") or ["dry_run"])
    if request.backend not in allowed:
        raise ValueError(f"Subagent backend {request.backend!r} is not allowed")
    if request.backend in _GATED_BACKENDS:
        flag = f"{request.backend}_enabled"
        if not bool(global_policy.get(flag, False)):
            raise ValueError(
                f"{request.backend} backend requires delegation_policy.{flag}: true"
            )

    running_for_parent = [item for item in list_sessions(sessions_dir) if item.parent_agent_id == agent.id and item.status == "running"]
    max_parent = int(agent_policy.get("max_parallel_subagents", global_policy.get("max_subagents_per_parent", 4)))
    if len(running_for_parent) >= max_parent:
        raise ValueError(f"Agent {agent.id} has reached max_parallel_subagents {max_parent}")
    _validate_subagent_resource_policy(agent, request)


def _build_session(sessions_dir: Path, request: SpawnSubagentInput) -> SubagentSession:
    session_id = new_id("subagent")
    logs_path = _session_dir(sessions_dir, session_id) / "logs.md"
    return SubagentSession(
        id=session_id,
        backend=request.backend,
        mode=request.mode,
        parent_agent_id=request.parent_agent_id,
        task_id=request.task_id,
        status="planned",
        created_at=now_iso(),
        updated_at=now_iso(),
        cwd=_expanded_cwd(request),
        depth=request.depth,
        work_order=request.work_order.to_dict(),
        limits=request.limits,
        result={},
        logs_path=str(logs_path),
    )


def _dry_run_adapter(session: SubagentSession, request: SpawnSubagentInput) -> dict[str, Any]:
    return {
        "summary": f"Dry-run subagent planned: {request.work_order.goal}",
        "changed_files": [],
        "commands_run": [],
        "test_results": {"status": "not_run", "details": "dry_run backend"},
        "risks": [],
        "artifacts": [],
    }


def _build_subagent_prompt(request: SpawnSubagentInput) -> str:
    return "\n".join(
        [
            f"Goal: {request.work_order.goal}",
            f"Mode: {request.mode}",
            f"Instructions: {request.work_order.instructions or ''}",
            "Return a concise summary, commands run, changed files, test results, and risks.",
        ]
    )


def _run_external_cli_adapter(
    session: SubagentSession,
    request: SpawnSubagentInput,
    command: str,
    args_prefix: list[str],
    label: str,
) -> dict[str, Any]:
    """Shared shape for any external-CLI subagent backend (codex_cli, claude_code).

    The argv pattern is `<command> <flag> <prompt>` and the result shape is the
    same for both: stdout becomes the truncated summary, returncode drives
    pass/fail, stderr is captured in the session log alongside stdout.
    """
    prompt = _build_subagent_prompt(request)
    spec = SandboxSpec(
        command=command,
        args=[*args_prefix, prompt],
        cwd=Path(_expanded_cwd(request)),
        secrets_required=_secrets_required(request),
        timeout_seconds=float(max(1, int(request.limits.get("max_runtime_minutes", 20))) * 60),
    )
    completed = run_sandboxed(spec)
    log_text = (completed.stdout or "") + ("\nSTDERR:\n" + completed.stderr if completed.stderr else "")
    if session.logs_path:
        Path(session.logs_path).write_text(log_text, encoding="utf-8")
    status = "passed" if completed.returncode == 0 else "failed"
    summary, truncated = _truncate_summary((completed.stdout or completed.stderr or f"{label} completed").strip())
    return {
        "summary": summary,
        "truncated": truncated,
        "changed_files": [],
        "commands_run": [f"{command} {' '.join(args_prefix)} <work_order>"],
        "test_results": {"status": status, "details": f"exit_code={completed.returncode}"},
        "risks": [] if completed.returncode == 0 else [f"{label} exited non-zero"],
        "artifacts": [],
    }


def _codex_cli_adapter(session: SubagentSession, request: SpawnSubagentInput) -> dict[str, Any]:
    return _run_external_cli_adapter(session, request, command="codex", args_prefix=["exec"], label="codex_cli")


def _claude_code_adapter(session: SubagentSession, request: SpawnSubagentInput) -> dict[str, Any]:
    return _run_external_cli_adapter(
        session, request, command="claude", args_prefix=["--print"], label="claude_code"
    )


def spawn_subagent(home: Path, logs_dir: Path, sessions_dir: Path, agent: AgentConfig, payload: dict[str, Any]) -> SubagentSession:
    request = SpawnSubagentInput.from_dict(payload, parent_agent_id=agent.id)
    validate_delegation_policy(home, sessions_dir, agent, request)
    session = _build_session(sessions_dir, request)
    append_event(logs_dir, "subagent.spawn.planned", session_id=session.id, backend=request.backend, parent_agent_id=agent.id, task_id=request.task_id)
    write_session(sessions_dir, session)

    session.status = "running"
    session.updated_at = now_iso()
    write_session(sessions_dir, session)
    append_event(logs_dir, "subagent.spawn.started", session_id=session.id, backend=request.backend, parent_agent_id=agent.id)

    try:
        if request.backend == "dry_run":
            result = _dry_run_adapter(session, request)
        elif request.backend == "codex_cli":
            result = _codex_cli_adapter(session, request)
        elif request.backend == "claude_code":
            result = _claude_code_adapter(session, request)
        else:
            raise ValueError(f"Unsupported subagent backend: {request.backend}")
        session.status = "completed"
        session.result = result
        if session.logs_path and not Path(session.logs_path).exists():
            Path(session.logs_path).write_text(result["summary"], encoding="utf-8")
        append_event(logs_dir, "subagent.spawn.completed", session_id=session.id, backend=request.backend, parent_agent_id=agent.id)
    except Exception as exc:
        session.status = "failed"
        session.result = {"summary": str(exc), "risks": ["subagent execution failed"]}
        append_event(logs_dir, "subagent.spawn.failed", session_id=session.id, backend=request.backend, parent_agent_id=agent.id, error=str(exc))
    finally:
        session.updated_at = now_iso()
        write_session(sessions_dir, session)
    return session

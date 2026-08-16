"""Workflow engine v2 — DAG runner with persisted, resumable run state.

A v2 workflow declares `nodes` + `edges` instead of a linear `steps` list.
Node types: `tool` (capability action, same dispatch as a v1 step), `llm`
(sugar for `draft_with_model`), `human_approval` (parks the run until the user
replies `approve <code>` on their channel), and `writeback` (copy a named
upstream output to a workspace file). Edges carry `on: success|error|always`
(default success), which is what gives workflows branching and `on_fail`
handling.

Run state is file-first: everything about a run lives in
`~/.jigga/runs/workflows/<workflow_id>/<run_id>/run.json` with per-node status
(pending / done / failed / awaiting_approval / skipped). A run is advanced —
never held in memory — so it survives restarts: the CLI advances it
synchronously as far as it can, and the supervisor heartbeat re-advances any
non-terminal run each tick (`advance_all_runs`). Approval parking reuses the
existing queue (`runtime/approvals.py`) and channel plumbing: the ask goes to
the owner conversation, the channel gateway resolves `approve <code>`, and the
next advance consumes the resolution exactly once.

Media nodes (image/video/audio) are deliberately not here yet — they land with
the media drivers parity work.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jigga.core.config import default_permission_mode, load_agents, load_runtime_config, load_workflows
from jigga.core.io import ensure_dir, read_json, write_json
from jigga.core.models import AgentConfig, WorkflowConfig, WorkflowNode, WorkflowStep, now_iso
from jigga.core.paths import JiggaPaths
from jigga.runtime.approvals import consume_if_approved, consume_if_denied, pending_approvals, request_approval
from jigga.runtime.audit import actor_context, append_event, current_trace_id, new_id, trace_context
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.channels import ADAPTERS, owner_conversation
from jigga.runtime.dispatcher import RuntimeContext, execute_step, register_outputs
from jigga.runtime.memory import build_context_package, write_memory_result
from jigga.runtime.notifications import NotificationRequest, delivery_mode, send_notification

NODE_TYPES = {"tool", "llm", "media", "human_approval", "writeback"}
EDGE_ON = {"success", "error", "always"}
_TERMINAL = {"done", "failed", "skipped"}
# Per-advance node budget: bounds a tick's work so one sprawling run can't
# monopolize the supervisor heartbeat.
DEFAULT_MAX_NODES_PER_ADVANCE = 10
DEFAULT_MAX_RUNS_PER_TICK = 8
# How long an approval may sit parked before the run is alarmed on. A human
# legitimately takes hours, so this is not a failure threshold — it's the point
# past which silence stops being evidence that anyone was ever asked.
DEFAULT_MAX_PARKED_HOURS = 24


# --- graph -----------------------------------------------------------------


def _edges(workflow: WorkflowConfig) -> list[dict[str, str]]:
    """Normalized edges: {"from", "to", "on"} with `on` defaulted to success."""
    normalized = []
    for edge in workflow.edges:
        normalized.append({
            "from": str(edge.get("from", "")),
            "to": str(edge.get("to", "")),
            "on": str(edge.get("on", "success")),
        })
    return normalized


def validate_graph(workflow: WorkflowConfig) -> list[str]:
    """Structural problems that make a v2 workflow unrunnable (empty = valid)."""
    problems: list[str] = []
    ids = [node.id for node in workflow.nodes]
    seen: set[str] = set()
    for node_id in ids:
        if node_id in seen:
            problems.append(f"duplicate node id: {node_id}")
        seen.add(node_id)
    for node in workflow.nodes:
        if node.type not in NODE_TYPES:
            problems.append(f"node {node.id}: unknown type {node.type!r} (allowed: {', '.join(sorted(NODE_TYPES))})")
        if node.type == "tool" and not node.action:
            problems.append(f"node {node.id}: tool node requires an action")
        if node.type == "writeback" and not (node.input or {}).get("path"):
            problems.append(f"node {node.id}: writeback node requires input.path")
    for edge in _edges(workflow):
        for end in ("from", "to"):
            if edge[end] not in seen:
                problems.append(f"edge {edge['from']}->{edge['to']}: unknown node {edge[end]!r}")
        if edge["on"] not in EDGE_ON:
            problems.append(f"edge {edge['from']}->{edge['to']}: on={edge['on']!r} (allowed: success, error, always)")
    # Cycle check (Kahn) — only meaningful once ids/refs are sane.
    if not problems:
        incoming: dict[str, int] = {node_id: 0 for node_id in seen}
        outgoing: dict[str, list[str]] = {node_id: [] for node_id in seen}
        for edge in _edges(workflow):
            incoming[edge["to"]] += 1
            outgoing[edge["from"]].append(edge["to"])
        queue = [node_id for node_id, count in incoming.items() if count == 0]
        visited = 0
        while queue:
            node_id = queue.pop()
            visited += 1
            for dst in outgoing[node_id]:
                incoming[dst] -= 1
                if incoming[dst] == 0:
                    queue.append(dst)
        if visited != len(seen):
            problems.append("workflow graph has a cycle")
    return problems


def _edge_fired(edge: dict[str, str], src_status: str) -> bool:
    if src_status == "skipped":
        return False
    if edge["on"] == "always":
        return src_status in {"done", "failed"}
    return src_status == ("done" if edge["on"] == "success" else "failed")


def _sweep(workflow: WorkflowConfig, nodes_state: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """One reachability sweep → (ready node ids, newly-skipped node ids).

    A node is ready when every incoming edge's source is terminal and at least
    one incoming edge fired (entry nodes: no incoming edges). It is skipped
    when all incoming edges resolved and none fired — its branch wasn't taken.
    Skips propagate on the next sweep because `skipped` is terminal.
    """
    edges = _edges(workflow)
    ready: list[str] = []
    skipped: list[str] = []
    for node in workflow.nodes:
        state = nodes_state[node.id]
        if state["status"] != "pending":
            continue
        incoming = [edge for edge in edges if edge["to"] == node.id]
        if not incoming:
            ready.append(node.id)
            continue
        statuses = [nodes_state[edge["from"]]["status"] for edge in incoming]
        if any(status not in _TERMINAL for status in statuses):
            continue
        if any(_edge_fired(edge, nodes_state[edge["from"]]["status"]) for edge in incoming):
            ready.append(node.id)
        else:
            skipped.append(node.id)
    return ready, skipped


# --- run state -------------------------------------------------------------


def _run_dir(paths: JiggaPaths, workflow_id: str, run_id: str) -> Path:
    return paths.runs / "workflows" / workflow_id / run_id


def _save(paths: JiggaPaths, record: dict[str, Any]) -> None:
    record["updated_at"] = now_iso()
    write_json(_run_dir(paths, record["workflow_id"], record["id"]) / "run.json", record)


def load_run(paths: JiggaPaths, run_id: str) -> dict[str, Any] | None:
    for path in (paths.runs / "workflows").glob(f"*/{run_id}/run.json"):
        try:
            return read_json(path)
        except (OSError, ValueError):
            return None
    return None


def list_runs(paths: JiggaPaths, workflow_id: str | None = None, *, active_only: bool = False) -> list[dict[str, Any]]:
    root = paths.runs / "workflows"
    pattern = f"{workflow_id}/*/run.json" if workflow_id else "*/*/run.json"
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob(pattern)) if root.exists() else []:
        try:
            record = read_json(path)
        except (OSError, ValueError):
            continue  # a half-written or corrupt run file must not hide the rest
        if record.get("engine") != "v2":
            continue
        if active_only and record.get("status") not in {"running", "awaiting_approval"}:
            continue
        records.append(record)
    return records


def start_run(paths: JiggaPaths, workflow: WorkflowConfig, *, trigger: dict[str, Any] | None = None) -> dict[str, Any]:
    problems = validate_graph(workflow)
    if problems:
        raise ValueError(f"Workflow {workflow.id} graph invalid: " + "; ".join(problems))
    run_id = new_id("workflow_run")
    ensure_dir(_run_dir(paths, workflow.id, run_id))
    record = {
        "id": run_id,
        "workflow_id": workflow.id,
        "engine": "v2",
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "completed_at": None,
        "run_dir": str(_run_dir(paths, workflow.id, run_id)),
        "trace_id": current_trace_id(),
        "trigger": trigger or {},
        "nodes": {node.id: {"status": "pending"} for node in workflow.nodes},
        "outputs": {},
    }
    _save(paths, record)
    append_event(paths.logs, "workflow.run.started", workflow=workflow.id, run_id=run_id, engine="v2")
    return record


# --- approvals -------------------------------------------------------------


def _approval_key(record: dict[str, Any], node_id: str) -> tuple[str, str]:
    """(task_id, action) identifying a run-node in the shared approval queue."""
    return (f"wfrun:{record['id']}", f"workflow_node:{record['workflow_id']}:{node_id}")


def _delivery_target(paths: JiggaPaths) -> tuple[str | None, Any, str | None]:
    """(channel, conversation_id, problem) for an approval ask to the owner.

    `problem` is None only when the ask can actually be delivered; otherwise it
    names why not. Resolved *before* the node parks so that "nobody has answered
    yet" and "nobody was ever asked" are different states rather than the same
    silence — the ambiguity that parked a run for 36 days on the precursor stack
    and took every downstream run with it.
    """
    owner = owner_conversation(paths.home)
    if owner is None:
        return None, None, "no owner conversation on any enabled channel"
    channel, conversation_id = owner
    if ADAPTERS.get(channel) is None:
        return channel, conversation_id, f"channel {channel!r} has no registered adapter"
    return channel, conversation_id, None


def _park_for_approval(paths: JiggaPaths, record: dict[str, Any], node: WorkflowNode, reason: str) -> None:
    task_id, action = _approval_key(record, node.id)
    channel, conversation_id, problem = _delivery_target(paths)
    approval = request_approval(
        paths.approvals, agent_id=node.agent or "workflow", task_id=task_id, action=action,
        reason=reason, channel=channel, conversation_id=conversation_id,
    )
    state = record["nodes"][node.id]
    state["status"] = "awaiting_approval"
    state["approval_code"] = approval["code"]
    state["parked_at"] = now_iso()
    append_event(paths.logs, "workflow.approval_requested", status="ask", workflow=record["workflow_id"],
                 run_id=record["id"], node=node.id, code=approval["code"], channel=channel)
    if problem is None:
        prompt = (node.input or {}).get("prompt") or reason
        text = (f"Workflow {record['workflow_id']} is waiting on node {node.id}.\n{prompt}\n\n"
                f"Reply: approve {approval['code']}   or   deny {approval['code']}")
        try:
            ADAPTERS[channel].send(paths.home, conversation_id=conversation_id, text=text)
            state["delivery"] = "delivered"
            return
        except Exception as exc:  # noqa: BLE001 — a send fault must not fail the run; the code still works via CLI
            problem = f"send failed: {exc}"
    # Undeliverable. The run still parks — `jigga approve <code>` always works —
    # but it parks *visibly*: the node carries the reason, the event is an
    # error, and `alarm_stale_approvals` escalates off-channel if it sits.
    state["delivery"] = "undelivered"
    state["delivery_error"] = problem
    append_event(paths.logs, "workflow.approval_undeliverable", status="error", workflow=record["workflow_id"],
                 run_id=record["id"], node=node.id, code=approval["code"], channel=channel, error=problem,
                 hint=f"run is parked; approve locally with `jigga approve {approval['code']}`")


def _max_parked_hours(home: Path) -> int:
    approvals = load_runtime_config(home).get("approvals") or {}
    return int(approvals.get("max_parked_hours", DEFAULT_MAX_PARKED_HOURS))


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def alarm_stale_approvals(paths: JiggaPaths, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Alarm once on each approval node parked past `approvals.max_parked_hours`.

    Deliberately routed to a desktop notification rather than back through the
    channel: an alarm must not depend on the subsystem it monitors, and the most
    likely reason an approval went unanswered is that the channel could not
    deliver the ask in the first place.

    Marks `stale_alarm_at` on the node so the alarm fires once per parked node
    rather than every heartbeat. Never changes run status — a parked run is
    still legitimately parked; this only makes the silence audible.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=_max_parked_hours(paths.home))
    dry_run = delivery_mode(paths.home) == "dry_run"
    alarmed: list[dict[str, Any]] = []
    for record in list_runs(paths, active_only=True):
        changed = False
        for node_id, state in (record.get("nodes") or {}).items():
            if state.get("status") != "awaiting_approval" or state.get("stale_alarm_at"):
                continue
            parked = _parse_iso(state.get("parked_at"))
            if parked is None or parked >= cutoff:
                continue
            state["stale_alarm_at"] = now_iso()
            changed = True
            undelivered = state.get("delivery") == "undelivered"
            code = state.get("approval_code")
            append_event(paths.logs, "workflow.approval_stale", status="error",
                         workflow=record.get("workflow_id"), run_id=record.get("id"), node=node_id,
                         code=code, parked_at=state.get("parked_at"), delivery=state.get("delivery"),
                         delivery_error=state.get("delivery_error"))
            body = (f"{record.get('workflow_id')} / {node_id} has been waiting since "
                    f"{state.get('parked_at')}."
                    + (" The request was never delivered." if undelivered else "")
                    + f" Approve with: jigga approve {code}")
            try:
                send_notification(
                    NotificationRequest(title="JIGGA: approval still waiting", body=body,
                                        urgency="critical" if undelivered else "normal"),
                    dry_run=dry_run,
                )
            except Exception as exc:  # noqa: BLE001 — the audit event is the durable alarm; delivery is best-effort
                append_event(paths.logs, "workflow.approval_alarm_error", status="error",
                             run_id=record.get("id"), node=node_id, error=str(exc))
            alarmed.append({"run_id": record.get("id"), "workflow": record.get("workflow_id"),
                            "node": node_id, "delivery": state.get("delivery")})
        if changed:
            record["updated_at"] = now_iso()
            write_json(Path(record["run_dir"]) / "run.json", record)
    return alarmed


def _approval_resolution(paths: JiggaPaths, record: dict[str, Any], node_id: str) -> str:
    """Resolution for a parked node: 'approved' / 'denied' (consumed exactly
    once), 'pending' while the ask is open, or 'missing' when the queue record
    is gone (e.g. cleaned up) and the ask must be re-issued."""
    task_id, action = _approval_key(record, node_id)
    if consume_if_approved(paths.approvals, task_id=task_id, action=action):
        return "approved"
    if consume_if_denied(paths.approvals, task_id=task_id, action=action):
        return "denied"
    if any(a.get("task_id") == task_id and a.get("action") == action for a in pending_approvals(paths.approvals)):
        return "pending"
    return "missing"


# --- node execution --------------------------------------------------------


# Sugar: a node type that implies its action, so a graph reads as what it does
# rather than as which capability it happens to call.
_NODE_TYPE_ACTIONS = {"llm": "draft_with_model", "media": "media.generate_image"}


def _node_step(node: WorkflowNode) -> WorkflowStep:
    action = node.action or _NODE_TYPE_ACTIONS.get(node.type, "")
    return WorkflowStep(id=node.id, action=action, agent=node.agent,
                        input=dict(node.input or {}), output=node.output, optional=node.optional,
                        # getattr: output contracts land in a separate PR, and
                        # these two must not depend on each other's merge order.
                        **({"output_fields": list(getattr(node, "output_fields", None) or [])}
                           if hasattr(WorkflowStep, "__dataclass_fields__")
                           and "output_fields" in WorkflowStep.__dataclass_fields__ else {}))


def _writeback_target(paths: JiggaPaths, raw: str) -> Path:
    """Canonicalized writeback destination, confined to `~/.jigga/workspaces/`
    — the shared file-first coordination area. Anything else (absolute paths,
    `..` escapes) is rejected: writeback is a coordination convenience, not a
    general filesystem capability (that exists, with policy gating)."""
    workspaces = (paths.home / "workspaces").resolve()
    target = (paths.home / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    if not target.is_relative_to(workspaces):
        raise ValueError(f"writeback path must be under workspaces/: {raw!r}")
    return target


def _execute_node(
    paths: JiggaPaths,
    record: dict[str, Any],
    node: WorkflowNode,
    agents: dict[str, AgentConfig],
    registry: CapabilityRegistry,
) -> None:
    from jigga.runtime.dispatcher import resolve_value

    state = record["nodes"][node.id]
    state["status"] = "running"
    state["started_at"] = now_iso()
    append_event(paths.logs, "workflow.node.started", workflow=record["workflow_id"], run_id=record["id"],
                 node=node.id, type=node.type)
    try:
        if node.type == "writeback":
            # resolve_value substitutes output names with their values (v1
            # chaining semantics), so `source: <name>` usually arrives already
            # resolved; a still-string source falls back to an outputs lookup.
            # An unresolved `${name}` raises here and fails the node — which is
            # the point: a writeback that silently wrote its own reference text
            # to disk is the corruption this exists to prevent.
            implicit: list[str] = []
            resolved = resolve_value(dict(node.input or {}), record["outputs"], implicit=implicit)
            for ref in implicit:
                append_event(paths.logs, "workflow.reference.implicit", status="ask",
                             workflow=record["workflow_id"], run_id=record["id"],
                             node=node.id, reference=ref,
                             hint=f"write it as ${{{ref}}}")
            content = resolved.get("value", resolved.get("source"))
            if isinstance(content, str):
                content = record["outputs"].get(content, content)
            target = _writeback_target(paths, str(resolved["path"]))
            ensure_dir(target.parent)
            if isinstance(content, str):
                target.write_text(content, encoding="utf-8")
            else:
                write_json(target, content)
            output: Any = {"path": str(target)}
        else:
            agent = agents.get(node.agent or "")
            scope = agent.memory_scope if agent and agent.memory_scope else "task_only"
            runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                                     sessions_dir=paths.home / "sessions")
            output, _artifact = execute_step(
                _node_step(node), Path(record["run_dir"]), record["outputs"],
                build_context_package(paths.memory, scope), runtime, registry,
                paths.logs, record["workflow_id"], record["id"],
            )
    except Exception as exc:  # noqa: BLE001 — a node fault fails the node (error edges may handle it), never the runner
        from jigga.runtime.model_router import error_category

        state["status"] = "failed"
        state["error"] = str(exc)
        # Assertion 12: a failed node must say what *kind* of failure it was.
        # "unknown" on an auth failure is itself the bug.
        state["error_category"] = error_category(exc)
        state["completed_at"] = now_iso()
        append_event(paths.logs, "workflow.node.failed", status="failed", workflow=record["workflow_id"],
                     run_id=record["id"], node=node.id, error=str(exc),
                     error_category=state["error_category"])
        return
    state["status"] = "done"
    state["completed_at"] = now_iso()
    register_outputs(record["outputs"], node, output)
    append_event(paths.logs, "workflow.node.completed", workflow=record["workflow_id"], run_id=record["id"],
                 node=node.id)


def _node_gate(node: WorkflowNode, agents: dict[str, AgentConfig], default_mode: str,
               registry: CapabilityRegistry) -> dict[str, Any]:
    # Lazy import: workflow.py routes v2 runs into this module, so the policy
    # helper is imported at call time to keep module import acyclic.
    from jigga.runtime.workflow import _step_policy

    return _step_policy(_node_step(node), None, agents, default_mode, registry)


# --- the runner ------------------------------------------------------------


def advance_run(
    paths: JiggaPaths,
    record: dict[str, Any],
    *,
    project_capabilities: Path | None = None,
    max_nodes: int = DEFAULT_MAX_NODES_PER_ADVANCE,
) -> dict[str, Any]:
    """Advance a run as far as it can go within `max_nodes`: resolve parked
    approvals, execute ready nodes, skip untaken branches, finalize when every
    node is terminal. Persists state after every transition, so a crash at any
    point resumes cleanly on the next advance."""
    if record.get("status") not in {"running", "awaiting_approval"}:
        return record
    workflow = load_workflows(paths.workflows).get(record["workflow_id"])
    if workflow is None:
        record["status"] = "failed"
        record["error"] = "workflow config removed"
        record["completed_at"] = now_iso()
        _save(paths, record)
        append_event(paths.logs, "workflow.run.failed", status="failed", workflow=record["workflow_id"],
                     run_id=record["id"], error="workflow config removed")
        return record
    agents = load_agents(paths.agents)
    registry = CapabilityRegistry.load(
        user_capabilities=paths.capabilities,
        project_capabilities=project_capabilities,
        approvals_dir=paths.policies,
    )
    default_mode = default_permission_mode(paths.home)
    nodes = {node.id: node for node in workflow.nodes}
    executed = 0

    # Parked nodes first: consume any approve/deny that arrived since last tick.
    for node_id, state in record["nodes"].items():
        if state["status"] != "awaiting_approval" or node_id not in nodes:
            continue
        resolution = _approval_resolution(paths, record, node_id)
        if resolution == "pending":
            continue  # still parked — don't touch run.json, or every tick looks like progress
        node = nodes[node_id]
        if resolution == "approved":
            if node.type == "human_approval":
                state["status"] = "done"
                state["completed_at"] = now_iso()
                record["outputs"][node_id] = {"approved": True}
                append_event(paths.logs, "workflow.node.completed", workflow=record["workflow_id"],
                             run_id=record["id"], node=node_id, approved=True)
            else:  # a risk-gated tool/llm node the user just approved
                _execute_node(paths, record, node, agents, registry)
                executed += 1
        elif resolution == "denied":
            state["status"] = "failed"
            state["error"] = "denied by user"
            state["completed_at"] = now_iso()
            append_event(paths.logs, "workflow.node.failed", status="failed", workflow=record["workflow_id"],
                         run_id=record["id"], node=node_id, error="denied by user")
        elif resolution == "missing":  # queue record cleaned up — re-ask, else the run parks forever
            _park_for_approval(paths, record, node, "approval record was removed; re-requesting")
        _save(paths, record)

    while executed < max_nodes:
        ready, newly_skipped = _sweep(workflow, record["nodes"])
        for node_id in newly_skipped:
            record["nodes"][node_id]["status"] = "skipped"
            append_event(paths.logs, "workflow.node.skipped", workflow=record["workflow_id"],
                         run_id=record["id"], node=node_id)
        if newly_skipped:
            _save(paths, record)
        if not ready:
            break
        for node_id in ready:
            if executed >= max_nodes:
                break
            node = nodes[node_id]
            if node.type == "human_approval":
                _park_for_approval(paths, record, node, (node.input or {}).get("prompt") or f"node {node.id}")
            elif node.type == "writeback":
                # Engine-internal, agent-less, and confined to workspaces/ by
                # _writeback_target — not subject to the capability gate (the
                # plan side mirrors this).
                _execute_node(paths, record, node, agents, registry)
                executed += 1
            else:
                gate = _node_gate(node, agents, default_mode, registry)
                if gate["status"] == "blocked":
                    record["nodes"][node_id]["status"] = "failed"
                    record["nodes"][node_id]["error"] = gate.get("reason") or "blocked by policy"
                    record["nodes"][node_id]["completed_at"] = now_iso()
                    append_event(paths.logs, "workflow.node.failed", status="failed",
                                 workflow=record["workflow_id"], run_id=record["id"], node=node_id,
                                 error=record["nodes"][node_id]["error"])
                elif gate["status"] == "needs_approval":
                    _park_for_approval(paths, record, node, gate.get("reason") or f"node {node.id}")
                elif gate["status"] == "skipped":  # optional node whose agent isn't installed
                    record["nodes"][node_id]["status"] = "skipped"
                    append_event(paths.logs, "workflow.node.skipped", workflow=record["workflow_id"],
                                 run_id=record["id"], node=node_id, reason="optional agent missing")
                else:
                    _execute_node(paths, record, node, agents, registry)
                    executed += 1
            _save(paths, record)

    _finalize(paths, workflow, record)
    return record


def _finalize(paths: JiggaPaths, workflow: WorkflowConfig, record: dict[str, Any]) -> None:
    statuses = {node_id: state["status"] for node_id, state in record["nodes"].items()}
    if any(status == "awaiting_approval" for status in statuses.values()):
        if record["status"] != "awaiting_approval":
            record["status"] = "awaiting_approval"
            append_event(paths.logs, "workflow.run.awaiting_approval", status="ask",
                         workflow=record["workflow_id"], run_id=record["id"])
        _save(paths, record)
        return
    if any(status not in _TERMINAL for status in statuses.values()):
        record["status"] = "running"  # budget ran out mid-graph; next advance continues
        _save(paths, record)
        return
    # All nodes terminal. A failure is "handled" when a fired error/always edge
    # consumed it (or the node was optional); an unhandled failure fails the run.
    edges = _edges(workflow)
    nodes = {node.id: node for node in workflow.nodes}
    unhandled = [
        node_id for node_id, status in statuses.items()
        if status == "failed"
        and not (node_id in nodes and nodes[node_id].optional)
        and not any(edge["from"] == node_id and _edge_fired(edge, "failed") for edge in edges)
    ]
    record["status"] = "failed" if unhandled else "completed"
    record["completed_at"] = now_iso()
    if unhandled:
        record["error"] = f"unhandled node failure: {', '.join(unhandled)}"
        append_event(paths.logs, "workflow.run.failed", status="failed", workflow=record["workflow_id"],
                     run_id=record["id"], nodes=unhandled)
    else:
        memory_artifact = None
        if workflow.memory.get("write_summary") or workflow.memory.get("write_raw"):
            memory_artifact = write_memory_result(
                paths.memory, paths.logs, "workflow_result",
                {"workflow": record["workflow_id"], "outputs": record["outputs"]},
                {"workflow": record["workflow_id"], "run_id": record["id"]},
            )
        record["memory_artifact"] = str(memory_artifact) if memory_artifact else None
        append_event(paths.logs, "workflow.run.completed", workflow=record["workflow_id"], run_id=record["id"])
        append_event(paths.logs, "workflow.completed", workflow=record["workflow_id"], run_id=record["id"],
                     title=workflow.name)
    _save(paths, record)


def run_workflow_v2(
    paths: JiggaPaths,
    workflow: WorkflowConfig,
    *,
    project_capabilities: Path | None = None,
    trigger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Start a v2 run and advance it as far as it will go right now. Returns the
    persisted record — status `completed`, `failed`, or `awaiting_approval`
    (parked; the supervisor heartbeat resumes it after `approve <code>`)."""
    record = start_run(paths, workflow, trigger=trigger)
    # No budget on the synchronous first pass: `jigga workflow run` should
    # finish an approval-free workflow in one call, like the v1 runner.
    return advance_run(paths, record, project_capabilities=project_capabilities, max_nodes=10_000)


def advance_all_runs(
    paths: JiggaPaths,
    *,
    max_runs: int = DEFAULT_MAX_RUNS_PER_TICK,
    max_nodes_per_run: int = DEFAULT_MAX_NODES_PER_ADVANCE,
) -> dict[str, Any]:
    """Supervisor hook: advance every non-terminal v2 run, oldest first,
    bounded per tick. Each run gets its own trace-inheriting advance; one
    run's fault is contained so the rest still advance."""
    advanced: list[dict[str, str]] = []
    active = sorted(list_runs(paths, active_only=True), key=lambda r: r.get("created_at", ""))
    for record in active[:max_runs]:
        before = record.get("status")
        try:
            with trace_context(record.get("trace_id")), \
                    actor_context(f"workflow:{record.get('workflow_id')}"):
                after = advance_run(paths, record, max_nodes=max_nodes_per_run)
        except Exception as exc:  # noqa: BLE001 — one broken run must not stall every other run on the heartbeat
            append_event(paths.logs, "workflow.advance_error", status="error",
                         run_id=record.get("id"), workflow=record.get("workflow_id"), error=str(exc))
            continue
        if after.get("status") != before or after.get("updated_at") != record.get("updated_at"):
            advanced.append({"run_id": after["id"], "workflow": after["workflow_id"], "status": after["status"]})
    return {"active": len(active), "advanced": advanced}

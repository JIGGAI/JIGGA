"""Ticket lanes — a per-team kanban vocabulary layered on the task queue (W3).

A "ticket" is a team task (`runtime/tasks.py`); a "lane" is the board column it
sits in (`task.lane`). The vocabulary is declared by the TEAM, not hardcoded:
the team yaml `lanes:` field is `True` (default lanes), a list of
`{id, description?, gate?}`, or absent/False (no board → tickets behave like
plain tasks). Moving a ticket is file-first and audited (a line per move in the
team's `shared-context/tickets.jsonl`, plus an audit event); the one enforced
rule is an optional per-lane `gate:` member — only they move a ticket OUT of
that lane. Lanes are orthogonal to `task.state` (the execution lifecycle).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jigga.core.config import load_teams
from jigga.core.io import append_jsonl
from jigga.core.models import TeamConfig, now_iso
from jigga.runtime.audit import append_event
from jigga.runtime.tasks import find_task, list_tasks, write_task
from jigga.runtime.workspaces import workspace_dir

DEFAULT_LANES: list[dict[str, str]] = [
    {"id": "backlog", "description": "Triaged and ready to pick up."},
    {"id": "working", "description": "Actively in progress."},
    {"id": "review", "description": "Done and awaiting review/QA."},
    {"id": "done", "description": "Accepted and closed."},
]


@dataclass
class Lane:
    id: str
    description: str | None = None
    gate: str | None = None  # only this member/role may move a ticket OUT of this lane


class LaneError(ValueError):
    """Invalid lane vocabulary, unknown lane, or non-team task."""


class LaneGateError(PermissionError):
    """A non-gate actor tried to move a ticket out of a gated lane."""


def team_member_names(team: TeamConfig) -> set[str]:
    """Every name a gate / actor may reference — member ids AND roles."""
    names: set[str] = set()
    for member in team.agents or []:
        if isinstance(member, dict):
            if member.get("id"):
                names.add(str(member["id"]))
            if member.get("role"):
                names.add(str(member["role"]))
    return names


def team_lanes(team: TeamConfig) -> list[Lane]:
    """Normalize a team's `lanes:` field into the declared vocabulary. Raises
    LaneError on a malformed block (duplicate ids, gate naming a non-member)."""
    raw = team.lanes
    if raw is True:
        entries: list[Any] = list(DEFAULT_LANES)
    elif not raw:
        return []
    elif isinstance(raw, list):
        entries = raw
    else:
        raise LaneError(f"team {team.id!r} `lanes:` must be true or a list (got {type(raw).__name__})")

    member_names = team_member_names(team)
    lanes: list[Lane] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            entry = {"id": entry}
        if not isinstance(entry, dict) or not entry.get("id"):
            raise LaneError(f"team {team.id!r} lane entry needs an id (got {entry!r})")
        lane_id = str(entry["id"])
        if lane_id in seen:
            raise LaneError(f"team {team.id!r} has a duplicate lane id {lane_id!r}")
        seen.add(lane_id)
        gate = entry.get("gate")
        if gate is not None:
            gate = str(gate)
            if member_names and gate not in member_names:
                raise LaneError(
                    f"team {team.id!r} lane {lane_id!r} gate {gate!r} is not a team member or role")
        description = entry.get("description")
        lanes.append(Lane(id=lane_id, description=str(description) if description else None, gate=gate))
    return lanes


def default_lane(team: TeamConfig) -> str | None:
    """The lane a new ticket lands in (the first declared lane), or None."""
    lanes = team_lanes(team)
    return lanes[0].id if lanes else None


def find_lane(team: TeamConfig, lane_id: str) -> Lane | None:
    return next((lane for lane in team_lanes(team) if lane.id == lane_id), None)


def _actor_identities(team: TeamConfig, actor: str | None) -> set[str]:
    """Every name `actor` answers to in this team — its own value plus, if it
    names a member, that member's id and role. So a gate of `strategy` is
    satisfied by the agent whose role is strategy, and vice-versa."""
    if not actor:
        return set()
    identities = {actor}
    for member in team.agents or []:
        if isinstance(member, dict) and actor in (member.get("id"), member.get("role")):
            if member.get("id"):
                identities.add(str(member["id"]))
            if member.get("role"):
                identities.add(str(member["role"]))
    return identities


def tickets_log_path(home: Path, team_id: str) -> Path:
    return workspace_dir(home, team_id) / "shared-context" / "tickets.jsonl"


def team_for_task(teams_dir: Path, task: Any) -> tuple[str, TeamConfig]:
    """(team_id, team) for a ticket; raises if the task isn't a team task or
    the team is gone."""
    team_id = (task.metadata or {}).get("team_id")
    if not team_id:
        raise LaneError(f"Task {task.id} is not a team task — it has no lane board.")
    team = load_teams(teams_dir).get(team_id)
    if team is None:
        raise LaneError(f"Team {team_id!r} (owner of task {task.id}) not found.")
    return team_id, team


def move_task_lane(
    home: Path, tasks_dir: Path, logs_dir: Path, teams_dir: Path,
    task_id: str, to_lane: str, *, actor: str | None = None,
) -> Any:
    """Move a ticket to `to_lane`, enforcing the source lane's gate. `actor` is
    the mover (CLI `--as`, or the acting agent's id). Audited to the team's
    tickets.jsonl + the event log. Returns the updated Task."""
    task = find_task(tasks_dir, task_id)
    if task is None:
        raise LaneError(f"Task not found: {task_id}")
    team_id, team = team_for_task(teams_dir, task)
    lanes = team_lanes(team)
    if not lanes:
        raise LaneError(f"Team {team_id!r} has no lanes configured.")
    if not any(lane.id == to_lane for lane in lanes):
        raise LaneError(
            f"Unknown lane {to_lane!r} for team {team_id!r}. Lanes: {', '.join(lane.id for lane in lanes)}")

    from_lane = task.lane
    current = next((lane for lane in lanes if lane.id == from_lane), None)
    if current and current.gate and current.gate not in _actor_identities(team, actor):
        raise LaneGateError(
            f"Lane {from_lane!r} is gated by {current.gate!r}: only they move a ticket out of it "
            f"(actor={actor or 'unspecified'}). Pass --as {current.gate}.")

    task.lane = to_lane
    task.updated_at = now_iso()
    write_task(tasks_dir, task)
    append_jsonl(tickets_log_path(home, team_id),
                 {"time": now_iso(), "team": team_id, "task_id": task.id,
                  "from": from_lane, "to": to_lane, "actor": actor})
    append_event(logs_dir, "team.ticket.moved", team=team_id, task_id=task.id,
                 from_lane=from_lane, to_lane=to_lane, actor=actor)
    return task


def team_tickets(tasks_dir: Path, team_id: str) -> list[Any]:
    """All tasks (tickets) owned by a team, oldest first."""
    return [t for t in list_tasks(tasks_dir) if (t.metadata or {}).get("team_id") == team_id]


def tickets_by_lane(team: TeamConfig, tasks_dir: Path) -> dict[str, list[Any]]:
    """Team tickets grouped by lane id (in declared lane order); an `unfiled`
    bucket catches tickets whose lane is unset/stale."""
    lanes = team_lanes(team)
    grouped: dict[str, list[Any]] = {lane.id: [] for lane in lanes}
    grouped["unfiled"] = []
    for ticket in team_tickets(tasks_dir, team.id):
        grouped.setdefault(ticket.lane or "unfiled", []).append(ticket)
    return grouped


def render_lanes(team: TeamConfig) -> str:
    """A compact 'Ticket lanes' block for the team's agent context — each lane's
    prose meaning + gate, so agents know what the columns mean."""
    lanes = team_lanes(team)
    if not lanes:
        return ""
    lines = ["Ticket lanes (move with the `tickets` capability):"]
    for lane in lanes:
        suffix = f"  [gate: {lane.gate}]" if lane.gate else ""
        meaning = f" — {lane.description}" if lane.description else ""
        lines.append(f"- {lane.id}{meaning}{suffix}")
    return "\n".join(lines)

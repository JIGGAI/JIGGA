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
from jigga.runtime.audit import actor_context, append_event
from jigga.runtime.tasks import (
    archive_task,
    destroy_task,
    find_task,
    list_tasks,
    write_task,
)
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


def _retire_ticket(
    home: Path, tasks_dir: Path, logs_dir: Path, teams_dir: Path,
    task_id: str, *, actor: str | None, destroy: bool,
) -> Any:
    """Shared body of archive/delete: enforce the lane gate, then retire.

    The gate is the point. If taking a ticket off the board were ungated, "QA
    has not passed this yet" would be one click from "then get rid of it", and
    the gate would bind only the people who move tickets the honest way. A task
    with no team has no board and no gate, so it just goes.
    """
    task = find_task(tasks_dir, task_id)
    archived_only = False
    if task is None and destroy:
        # Deleting something already archived: off the board, so no gate left
        # to enforce — but still worth an audit line.
        archived_only = True
    elif task is None:
        raise LaneError(f"Task not found: {task_id}")

    team_id: str | None = None
    if not archived_only and (task.metadata or {}).get("team_id"):
        team_id, team = team_for_task(teams_dir, task)
        current = next((lane for lane in team_lanes(team) if lane.id == task.lane), None)
        if current and current.gate and current.gate not in _actor_identities(team, actor):
            verb = "deleting" if destroy else "archiving"
            raise LaneGateError(
                f"Lane {task.lane!r} is gated by {current.gate!r}: only they take a ticket out of "
                f"it, including by {verb} (actor={actor or 'unspecified'}). "
                f"Pass --as {current.gate}.")

    try:
        task = destroy_task(tasks_dir, task_id) if destroy else archive_task(tasks_dir, task_id)
    except ValueError as exc:
        raise LaneError(str(exc)) from exc

    event = "team.ticket.deleted" if destroy else "team.ticket.archived"
    if team_id:
        append_jsonl(tickets_log_path(home, team_id),
                     {"time": now_iso(), "team": team_id, "task_id": task.id,
                      "from": task.lane, "to": None, "actor": actor,
                      "deleted" if destroy else "archived": True})
    # Bind the actor for the event's top-level `actor` field, which is what
    # `jigga audit --actor` filters on — `append_event` reads it from the
    # context, not from a keyword. (`move_task_lane` above still records the
    # actor only inside details; worth aligning separately.)
    if actor:
        with actor_context(actor):
            append_event(logs_dir, event, team=team_id, task_id=task.id,
                         lane=task.lane, title=task.title, actor=actor)
    else:
        append_event(logs_dir, event, team=team_id, task_id=task.id,
                     lane=task.lane, title=task.title, actor=actor)
    return task


def archive_ticket(home: Path, tasks_dir: Path, logs_dir: Path, teams_dir: Path,
                   task_id: str, *, actor: str | None = None) -> Any:
    """Take a ticket off the board, keeping the file (recoverable)."""
    return _retire_ticket(home, tasks_dir, logs_dir, teams_dir, task_id,
                          actor=actor, destroy=False)


def delete_ticket(home: Path, tasks_dir: Path, logs_dir: Path, teams_dir: Path,
                  task_id: str, *, actor: str | None = None) -> Any:
    """Delete a ticket outright. Nothing survives — see `archive_ticket` for the
    recoverable version."""
    return _retire_ticket(home, tasks_dir, logs_dir, teams_dir, task_id,
                          actor=actor, destroy=True)


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


# --- lane transitions -------------------------------------------------------
#
# The destination lane is derived from the TRANSITION (who handed the ticket to
# whom), not from the target role alone, because a role can own several lanes:
# the lead owns `backlog` (work bounced back), `ready-for-pr` (QA passed) and
# `done`. "Assign to lead" therefore does not identify a lane; "test handed it
# to lead" does.

DEFAULT_LANE_TRANSITIONS: list[dict[str, str]] = [
    {"from": "lead", "to": "dev", "lane": "in-progress"},
    {"from": "dev", "to": "test", "lane": "testing"},
    {"from": "test", "to": "dev", "lane": "in-progress"},    # QA rejected
    {"from": "test", "to": "lead", "lane": "ready-for-pr"},  # QA passed
]
DEFAULT_BOUNCE_LANE = "backlog"


def role_of(team: TeamConfig, agent_id: str) -> str | None:
    """The team role (`dev`, `test`, `lead`, ...) for an agent id."""
    for member in team.agents or []:
        if isinstance(member, dict) and member.get("id") == agent_id:
            role = member.get("role")
            return str(role) if role else None
    return None


def lane_transitions(team: TeamConfig) -> dict[str, Any]:
    """The team's transition table, defaulted to the standard pipeline.

    Defaults are filtered against the team's actual lanes — a board without a
    `testing` lane must not be handed one — and a team that declares its own
    `rules` replaces the defaults outright rather than merging, so a custom
    board is exactly what it says it is.
    """
    known = {lane.id for lane in team_lanes(team)}
    declared = getattr(team, "lane_transitions", None)
    declared = declared if isinstance(declared, dict) else {}

    rules = declared.get("rules")
    if not isinstance(rules, list):
        rules = DEFAULT_LANE_TRANSITIONS
    rules = [r for r in rules if isinstance(r, dict) and r.get("lane") in known]

    bounce = declared.get("bounce_lane", DEFAULT_BOUNCE_LANE)
    if bounce not in known:
        bounce = None
    return {"rules": rules, "bounce_lane": bounce}


def derive_lane(team: TeamConfig, from_agent: str | None, to_agent: str) -> str | None:
    """The lane a handoff moves the ticket into, or None when no rule matches.

    None is not an error — the caller leaves the lane alone and says so, rather
    than guessing a destination.
    """
    from_role = role_of(team, from_agent) if from_agent else None
    to_role = role_of(team, to_agent)
    if not to_role:
        return None
    for rule in lane_transitions(team)["rules"]:
        if rule.get("to") == to_role and (rule.get("from") in (None, from_role)):
            return str(rule["lane"])
    return None

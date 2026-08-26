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

# The terminal lane. `lane == DONE_LANE` is what makes a ticket `completed`
# (see runtime/ticket_outcome.py), so it is the one lane an ordinary move may
# not target — `tickets.close` is its only door.
DONE_LANE = "done"

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

    # `done` is deliberately not assignment-driven: reaching it is what marks a
    # ticket `completed`, so an ordinary move into it would be a second, ungated
    # door into completion — no lead check, no ready-for-pr check. A dev holding
    # `tickets.move` walked straight through it in production. `tickets.close`
    # is the only route, and it audits its own refusals.
    if to_lane == DONE_LANE and is_lifecycle_managed(team):
        append_event(logs_dir, "team.ticket.move.refused", status="deny", team=team_id,
                     task_id=task.id, from_lane=task.lane, to_lane=to_lane, actor=actor,
                     reason="done is reached by closing a ticket, not by moving it")
        raise LaneGateError(
            f"Lane {DONE_LANE!r} is not reachable by a move: it is what marks a ticket "
            f"completed. Use tickets.close (team lead, from the close lane) instead "
            f"(actor={actor or 'unspecified'}).")

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
    prose meaning + gate, so agents know what the columns mean, plus (for a
    lifecycle-managed board) how work actually moves between them.

    The rules are GENERATED from the team's own transition table rather than
    written into a workspace file or a recipe, because an authored copy drifts:
    it has to be re-stated per team, and it silently goes stale the moment a
    team's roster or lanes change. Generating it means every board describes
    itself, and a new team is correct on its first wake with nothing to sync.
    """
    lanes = team_lanes(team)
    if not lanes:
        return ""
    lines = ["Ticket lanes (move with the `tickets` capability):"]
    for lane in lanes:
        suffix = f"  [gate: {lane.gate}]" if lane.gate else ""
        meaning = f" — {lane.description}" if lane.description else ""
        lines.append(f"- {lane.id}{meaning}{suffix}")

    if not is_lifecycle_managed(team):
        return "\n".join(lines)

    # How work moves. A lead holding a ticket reached for `task.assign` twice in
    # a row and put duplicates on the board, because that is the familiar verb
    # and nothing it was shown named the alternative at the point of decision.
    by_role = {r: [] for r in {rule["from"] for rule in lane_transitions(team)["rules"]}}
    for rule in lane_transitions(team)["rules"]:
        by_role.setdefault(rule["from"], []).append(rule)
    ids = {str(a.get("role")): str(a.get("id")) for a in (team.agents or [])
           if isinstance(a, dict) and a.get("id") and a.get("role")}

    lines.append("")
    lines.append("One ticket per piece of work — it travels the whole board.")
    lines.append("Hand it on with `tickets.handoff(ticket, assignee, comment)`; the lane moves")
    lines.append("for you. Never use `task.assign` to pass along work that already has a")
    lines.append("ticket — that abandons yours and puts a duplicate on the board.")
    for role in sorted(by_role):
        for rule in by_role[role]:
            frm, to = ids.get(rule["from"], rule["from"]), ids.get(rule["to"], rule["to"])
            lines.append(f"- {frm} -> {to}  lands in {rule['lane']}")
    closer = ids.get("lead", "the lead")
    lines.append(f"Only {closer} closes, with `tickets.close`, and only from "
                 f"{close_lane(team) or DEFAULT_CLOSE_LANE}. Nothing else completes a ticket.")
    lines.append("A run that ends without handing the ticket on bounces it back to the lead.")
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
# Used only when a team declares no `-> lead` rule to derive its close lane from.
DEFAULT_CLOSE_LANE = "ready-for-pr"


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


def close_lane(team: TeamConfig) -> str | None:
    """The lane a ticket closes FROM — the one a `-> lead` rule targets.

    Derived rather than hardcoded: `ready-for-pr` is `engineering-team`'s name
    for "QA passed", not a universal one, and a team that renames it must not
    lose its only exit.
    """
    for rule in lane_transitions(team)["rules"]:
        if rule.get("to") == "lead" and rule.get("lane"):
            return str(rule["lane"])
    return None


def is_lifecycle_managed(team: TeamConfig) -> bool:
    """Whether this team's board can actually run the ticket lifecycle.

    The lifecycle needs all four parts: a board, at least one usable transition
    rule to move a ticket along it, a bounce lane to catch unhandled work, and
    the terminal `done` lane that means `completed`. A lane-bearing team missing
    any of them (a marketing board of brief/drafting/review/published; the
    `lanes: true` shorthand, whose defaults share no lane with the pipeline
    rules) is NOT running this lifecycle, and the runtime must leave it on its
    previous behaviour rather than half-applying rules its board cannot satisfy
    — which would send every completed run straight to `blocked`.
    """
    lanes = team_lanes(team)
    if not lanes:
        return False
    if not any(lane.id == DONE_LANE for lane in lanes):
        return False
    transitions = lane_transitions(team)
    return bool(transitions["rules"]) and bool(transitions["bounce_lane"])


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

"""Break one complex ticket into linked story tickets.

The lead had no way to split work. `tickets.handoff` gives the whole ticket to
one agent; `task.assign` creates an unrelated ticket and is refused outright
while the lead holds a lane-managed one — which is exactly this situation. So a
complex ask went to a single dev as a single ticket, or nowhere.

This is a separate verb rather than a hole in that refusal, because the refusal
exists to remove a judgment call, and a carve-out would put it back at the
moment the model is already reaching for the wrong tool.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_teams
from jigga.runtime.lanes import (
    DEFAULT_CLOSE_LANE,
    close_lane,
    derive_lane,
    is_lifecycle_managed,
    lane_transitions,
    role_of,
    team_lanes,
)
from jigga.runtime.tasks import create_task, find_task, update_task

# A confused lead should not be able to flood the board.
MAX_STORIES = 20


class DecomposeError(ValueError):
    """A decomposition that must not happen. The message is shown to the agent."""


def _lead_of(team) -> str | None:
    for member in team.agents or []:
        if isinstance(member, dict) and member.get("role") == "lead" and member.get("id"):
            return str(member["id"])
    return None


def _builder_of(team) -> str | None:
    """The team's builder — not a hardcoded role name, but whoever the lead's
    own transition rule hands off to. A team whose builder role is `engineer`
    or `builder` derives its work lane exactly as well as one that calls it
    `dev`: this is the same class of assumption `DEFAULT_LANE_TRANSITIONS`
    was removed from lanes.py for, and hardcoding "dev" here would put it
    right back."""
    lead_id = _lead_of(team)
    if not lead_id:
        return None
    lead_role = role_of(team, lead_id)
    builder_role = next(
        (rule.get("to") for rule in lane_transitions(team)["rules"]
         if rule.get("from") == lead_role),
        None)
    if not builder_role:
        return None
    for member in team.agents or []:
        if isinstance(member, dict) and member.get("role") == builder_role and member.get("id"):
            return str(member["id"])
    return None


def _work_lane(team) -> str | None:
    """Where an epic sits while its stories are built.

    Derived from the team's own lead-to-builder rule, never hardcoded: core
    stopped asserting board shapes when DEFAULT_LANE_TRANSITIONS came out of
    lanes.py, and writing "in-progress" here would put one straight back.
    """
    lead, builder = _lead_of(team), _builder_of(team)
    if not lead or not builder:
        return None
    return derive_lane(team, lead, builder)


def _render_epic(original: str | None, summary: str, plan: str,
                 stories: list[tuple[str, dict]]) -> str:
    """The epic reads as a status page: what the plan is, where the full one
    lives, and what it was cut into. A path alone would make the board
    unreadable without a second lookup, and the plan file is not injected into
    anyone's context."""
    lines = ["## Plan", summary.strip(), "", f"Full plan: {plan}", "", "## Stories"]
    for sid, spec in stories:
        lines.append(f"- {sid}  {spec['title']}  -> {spec['assignee']}")
    if original and original.strip():
        lines += ["", "## Original request", original.strip()]
    return "\n".join(lines)


def decompose(tasks_dir: Path, teams_dir: Path, *, ticket_id: str, actor: str | None,
              summary: str, plan: str, stories: list[dict[str, Any]]) -> dict[str, Any]:
    """Create one story ticket per entry, link them to the epic, and park it."""
    epic = find_task(tasks_dir, ticket_id)
    if epic is None:
        raise DecomposeError(f"Ticket not found: {ticket_id}")
    team_id = (epic.metadata or {}).get("team_id")
    team = load_teams(teams_dir).get(team_id) if team_id else None
    if team is None or not is_lifecycle_managed(team):
        raise DecomposeError(
            f"Ticket {ticket_id} is not on a lifecycle-managed board; there is nothing to "
            "decompose into.")
    if role_of(team, actor or "") != "lead":
        raise DecomposeError("Only the team lead decomposes a ticket.")
    if (epic.metadata or {}).get("children"):
        raise DecomposeError(f"Ticket {ticket_id} has already been decomposed.")
    if not summary or not summary.strip():
        raise DecomposeError("A plan summary is required — the epic has to read on its own.")
    if not plan or not plan.strip():
        raise DecomposeError("A path to the full plan is required.")
    if not stories:
        raise DecomposeError("Decomposing needs at least one story.")
    if len(stories) > MAX_STORIES:
        raise DecomposeError(f"At most {MAX_STORIES} stories; got {len(stories)}.")

    members = {str(m.get("id")) for m in (team.agents or [])
               if isinstance(m, dict) and m.get("id")}
    for spec in stories:
        # A story that is not an object at all. Without this the first
        # `spec.get` raised AttributeError — not a DecomposeError, so the
        # handler's `except DecomposeError` missed it and the refusal was
        # never written to the audit log. Every guardrail here is auditable;
        # this one has to be too.
        if not isinstance(spec, dict):
            raise DecomposeError(
                f"Every story must be an object with title, description and assignee; got "
                f"{type(spec).__name__} {spec!r}. A bare list of titles carries no assignee "
                "and no brief.")
        if not str(spec.get("title") or "").strip():
            raise DecomposeError("Every story needs a title.")
        if not str(spec.get("description") or "").strip():
            raise DecomposeError(
                f"Story {spec.get('title')!r} needs a description: the brief the assignee "
                "works from, with its acceptance check.")
        if str(spec.get("assignee") or "") not in members:
            raise DecomposeError(
                f"Story {spec.get('title')!r} is assigned to {spec.get('assignee')!r}, "
                "a stranger to this team.")

    lanes = team_lanes(team)
    first_lane = lanes[0].id if lanes else None
    created: list[tuple[str, dict]] = []
    for spec in stories:
        story = create_task(
            tasks_dir, str(spec["title"]), description=str(spec["description"]),
            assignee=str(spec["assignee"]), lane=first_lane,
            metadata={"team_id": team_id, "parent": epic.id, "assigned_by": actor})
        created.append((story.id, spec))

    metadata = dict(epic.metadata or {})
    metadata["children"] = [sid for sid, _ in created]
    metadata["plan"] = plan
    lane = _work_lane(team)
    update_task(tasks_dir, epic.id, state="waiting", metadata=metadata,
                description=_render_epic(epic.description, summary, plan, created),
                **({"lane": lane} if lane else {}))
    return {"epic": epic.id, "stories": [sid for sid, _ in created], "lane": lane}


# States a child can be in that mean it will never complete on its own.
_DEAD_CHILD_STATES = {"failed", "blocked"}

# Every state a run can leave a story in for good. The release has to fire on
# ALL of them, not just `completed`: nothing ever moves a `failed` or `blocked`
# ticket again — `tasks_for_agent` selects only `pending` and the stale sweep
# only `claimed`/`running` — so an epic whose last story died would wait for a
# completion event that can never arrive.
TERMINAL_CHILD_STATES = {"completed"} | _DEAD_CHILD_STATES


def release_parent_if_ready(tasks_dir: Path, teams_dir: Path,
                            child_id: str) -> dict[str, Any] | None:
    """Wake a waiting epic when its children are finished, or one of them died.

    Called whenever a task reaches a terminal state. Returns None when there is
    nothing to do, which is the common case — most tasks have no parent.

    A failed or blocked child releases the epic immediately rather than leaving
    it asleep: one dead story would otherwise park the ask forever, and a
    silently stalled ticket is the failure this whole design removes.
    """
    child = find_task(tasks_dir, child_id)
    if child is None:
        return None
    parent_id = (child.metadata or {}).get("parent")
    if not parent_id:
        return None
    epic = find_task(tasks_dir, parent_id)
    if epic is None or epic.state != "waiting":
        return None      # already released; this runs on every child completion

    # (id, Task | None) — a missing task (archived or deleted) has no Task to
    # read a state from, so the id has to travel alongside the lookup or it is
    # lost the moment find_task returns None.
    children = [(cid, find_task(tasks_dir, cid))
                for cid in (epic.metadata or {}).get("children") or []]
    dead = [(cid, task) for cid, task in children
            if task is None or task.state in _DEAD_CHILD_STATES]
    if dead:
        cid, task = dead[0]
        # A gone child fires no future completion event, so treating it as
        # anything but dead would park the epic forever — the exact stall
        # this function exists to prevent. Releasing early is recoverable;
        # the lead sees it and can hand it back. Waiting is not.
        reason = f"{cid} is gone (archived or deleted)" if task is None \
            else f"{cid} ended {task.state}"
    elif all(task is not None and task.state == "completed" for _cid, task in children):
        reason = "children complete"
    else:
        return None

    team_id = (epic.metadata or {}).get("team_id")
    team = load_teams(teams_dir).get(team_id) if team_id else None
    lane = (close_lane(team) or DEFAULT_CLOSE_LANE) if team is not None else None
    update_task(tasks_dir, epic.id, state="pending", assignee=_lead_of(team) if team else epic.assignee,
                **({"lane": lane} if lane else {}))
    return {"epic": epic.id, "reason": reason}

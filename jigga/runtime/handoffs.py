"""Team handoffs — file-first, auditable agent-to-agent coordination (H3).

A team's `routing.handoffs` declares `{from, to, when}` rules. Until now the
team runtime only *logged* that handoffs were declared; this module *executes*
them. When the `from` member completes its team task, matching handoffs fire: a
task is created for the `to` member, and the handoff is appended to the team's
decision log at `shared-context/handoffs.jsonl`. No ephemeral bus — every
handoff is a file you can read and audit, consistent with the file-first
coordination direction.

Firing is completion-driven and deterministic: completing a team task IS the
signal, so every outgoing handoff from that member fires (a member's *incoming*
signal is not the same as what it *emits*, so we don't filter outgoing handoffs
by the incoming `when`). The `when` label names the handoff and is recorded.
An explicit `signal` may be passed (CLI / future agent-emitted signals) to fire
only handoffs whose `when` matches.

A hop counter rides on each handoff-created task; once it reaches `max_hops` the
chain is refused and logged, so a cyclic routing graph can't loop forever.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_runtime_config, load_teams
from jigga.core.io import append_jsonl
from jigga.core.models import TeamConfig, now_iso
from jigga.runtime.audit import append_event
from jigga.runtime.tasks import create_task
from jigga.runtime.workspaces import workspace_dir

DEFAULT_MAX_HOPS = 25


def handoffs_log_path(home: Path, team_id: str) -> Path:
    return workspace_dir(home, team_id) / "shared-context" / "handoffs.jsonl"


def _max_hops(home: Path) -> int:
    teams_cfg = load_runtime_config(home).get("teams") or {}
    try:
        return int(teams_cfg.get("handoff_max_hops", DEFAULT_MAX_HOPS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_HOPS


def eligible_handoffs(team: TeamConfig, from_member: str, signal: str | None) -> list[dict[str, Any]]:
    """Handoff rules that should fire for `from_member`. With no `signal`, every
    outgoing handoff from the member is eligible; with a signal, only rules whose
    `when` matches it."""
    routing = team.routing if isinstance(team.routing, dict) else {}
    rules = routing.get("handoffs")
    # Tolerate malformed config: handoffs not a list, or entries that aren't
    # dicts. This runs on the agent-completion path inside the supervisor tick,
    # so a bad team YAML must not be able to crash the heartbeat.
    if not isinstance(rules, list):
        return []
    out: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("from") != from_member or not rule.get("to"):
            continue
        if signal is not None and rule.get("when") != signal:
            continue
        out.append(rule)
    return out


def fire_handoffs(
    home: Path,
    logs_dir: Path,
    tasks_dir: Path,
    teams_dir: Path,
    *,
    team_id: str,
    from_member: str,
    signal: str | None = None,
    hops: int = 0,
    evidence: str | None = None,
) -> list[dict[str, Any]]:
    """Execute the handoffs triggered by `from_member` in `team_id`. Creates a
    task per eligible handoff, records each in the decision log, emits audit
    events, and returns the created task dicts. No-op (returns []) when the team
    or its handoffs don't apply."""
    teams = load_teams(teams_dir)
    team = teams.get(team_id)
    if team is None:
        return []
    rules = eligible_handoffs(team, from_member, signal)
    if not rules:
        return []

    max_hops = _max_hops(home)
    if hops >= max_hops:
        append_event(logs_dir, "team.handoff.blocked", status="ask", team=team_id,
                     from_member=from_member, hops=hops, max_hops=max_hops,
                     reason="max_hops_exceeded")
        return []

    created: list[dict[str, Any]] = []
    for rule in rules:
        to = rule["to"]
        when = rule.get("when")
        task = create_task(
            tasks_dir,
            title=f"Handoff from {from_member}" + (f": {when}" if when else ""),
            description=(f"{from_member} handed off to {to}"
                         + (f" on '{when}'." if when else ".")
                         + (f"\nEvidence: {evidence}" if evidence else "")),
            assignee=to,
            metadata={
                "team_id": team_id,
                "handoff_from": from_member,
                "handoff_when": when,
                "handoff_hops": hops + 1,
                "evidence": evidence,
            },
        )
        record = {"time": now_iso(), "team": team_id, "from": from_member, "to": to,
                  "when": when, "task_id": task.id, "hops": hops + 1, "evidence": evidence}
        append_jsonl(handoffs_log_path(home, team_id), record)
        append_event(logs_dir, "team.handoff.fired", team=team_id, from_member=from_member,
                     to=to, when=when, task_id=task.id, hops=hops + 1)
        created.append(task.to_dict())
    return created


def read_decision_log(home: Path, team_id: str) -> list[dict[str, Any]]:
    """The team's handoff decision log, oldest first."""
    from jigga.core.io import read_jsonl

    return read_jsonl(handoffs_log_path(home, team_id))

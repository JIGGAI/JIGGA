"""Fail-fast config validation (issue #59).

The runtime *tolerates* malformed config (a bad cron simply never fires; a
malformed `routing.handoffs` is ignored) so a single bad file can't crash the
supervisor tick. This module is the other half: catch those problems at
**authoring time** — `jigga validate` reports them, and `jigga team scaffold`
refuses to write a recipe that produces them — so the author gets a clear error
instead of an agent that mysteriously never wakes or never hands off.

Errors are returned as human-readable strings; warnings are prefixed `warning:`
(non-fatal — e.g. a handoff referencing a non-member).
"""

from __future__ import annotations

from typing import Any

from jigga.core.models import AgentConfig, TeamConfig

# (low, high) inclusive ranges for the 5 cron fields.
_CRON_FIELDS: list[tuple[str, int, int]] = [
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
]
_DOW_NAMES = {"SUN": "0", "MON": "1", "TUE": "2", "WED": "3", "THU": "4", "FRI": "5", "SAT": "6"}


def _int_in_range(token: str, lo: int, hi: int) -> str | None:
    if not (token.lstrip("-").isdigit()):
        return f"{token!r} is not a number"
    value = int(token)
    if not lo <= value <= hi:
        return f"{token!r} is out of range {lo}-{hi}"
    return None


def _validate_cron_field(field: str, lo: int, hi: int, *, is_dow: bool) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step = field[2:]
        if not step.isdigit() or int(step) == 0:
            return "step must be a positive integer (*/n, n>0)"
        return None
    def _norm(atom: str) -> str:
        if is_dow:
            atom = _DOW_NAMES.get(atom.strip().upper(), atom.strip())
            atom = "0" if atom == "7" else atom  # cron allows 7 == Sunday
        return atom

    for token in field.split(","):
        tok = token.strip()
        if not tok:
            return "empty list item"
        atoms = tok.split("-", 1) if "-" in tok.lstrip("-") else [tok]
        for atom in atoms:
            err = _int_in_range(_norm(atom), lo, hi)
            if err:
                return err
    return None


def validate_cron(cron: str) -> str | None:
    """Return an error string if `cron` is not a usable 5-field cron, else None."""
    parts = str(cron).split()
    if len(parts) != 5:
        return f"cron must have 5 fields, got {len(parts)}: {cron!r}"
    for value, (name, lo, hi) in zip(parts, _CRON_FIELDS, strict=True):
        err = _validate_cron_field(value, lo, hi, is_dow=(name == "day-of-week"))
        if err:
            return f"{name} field {value!r}: {err}"
    return None


def validate_agent(agent: AgentConfig) -> list[str]:
    problems: list[str] = []
    schedules = (agent.wake or {}).get("schedules") if isinstance(agent.wake, dict) else None
    for i, sched in enumerate(schedules or []):
        cron = sched.get("cron") if isinstance(sched, dict) else None
        if cron:
            err = validate_cron(cron)
            if err:
                problems.append(f"agent {agent.id}: wake.schedules[{i}] {err}")
    return problems


def validate_team(team: TeamConfig) -> list[str]:
    problems: list[str] = []
    if team.routing and not isinstance(team.routing, dict):
        return [f"team {team.id}: routing must be a mapping, got {type(team.routing).__name__}"]
    handoffs = (team.routing or {}).get("handoffs")
    if handoffs is None:
        return problems
    if not isinstance(handoffs, list):
        return [f"team {team.id}: routing.handoffs must be a list, got {type(handoffs).__name__}"]
    member_ids = {str(a.get("id")) for a in team.agents if isinstance(a, dict) and a.get("id")}
    for i, rule in enumerate(handoffs):
        loc = f"team {team.id}: routing.handoffs[{i}]"
        if not isinstance(rule, dict):
            problems.append(f"{loc} must be a mapping, got {type(rule).__name__}")
            continue
        frm, to = rule.get("from"), rule.get("to")
        if not frm:
            problems.append(f"{loc} is missing 'from'")
        if not to:
            problems.append(f"{loc} is missing 'to'")
        if rule.get("when") is not None and not isinstance(rule.get("when"), str):
            problems.append(f"{loc}.when must be a string")
        if member_ids:
            if frm and str(frm) not in member_ids:
                problems.append(f"warning: {loc}.from {frm!r} is not a team member")
            if to and str(to) not in member_ids:
                problems.append(f"warning: {loc}.to {to!r} is not a team member")
    return problems


def validate_configs(agents: dict[str, AgentConfig], teams: dict[str, TeamConfig]) -> list[str]:
    """All problems across the given agents + teams (errors and `warning:` lines)."""
    problems: list[str] = []
    for agent in agents.values():
        problems.extend(validate_agent(agent))
    for team in teams.values():
        problems.extend(validate_team(team))
    return problems


def is_error(problem: str) -> bool:
    return not problem.startswith("warning:")


def raise_if_errors(problems: list[str], *, context: str) -> None:
    """Raise ValueError listing any error-level problems (warnings don't block)."""
    errors = [p for p in problems if is_error(p)]
    if errors:
        raise ValueError(f"{context}:\n  - " + "\n  - ".join(errors))

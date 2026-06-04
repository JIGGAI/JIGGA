"""Agent context pack — assembles an agent's system prompt from layered files,
adopting the OpenClaw/ClawRecipes model.

When an agent wakes, it should already know **who it is, who it's helping, what
it's for, what it knows, and what the team is doing** — not be a per-task
amnesiac. This module gathers that from a fixed set of layers and concatenates
them into the system prompt (the "inject" half of the hybrid approach; the agent
can still read deeper files — full dated logs, tickets — via its tools).

Each layer is **generate-unless-authored**: an authored file on disk overrides
the generated default; layers with neither are skipped (the registry-style
"include what's there" pattern). Everything is a file you can read — file-first
and auditable.

Layers, in order (★ = private — omitted in a restricted/shared context for
prompt-injection / leak safety, mirroring OpenClaw's "MEMORY.md only in main
session" rule; the `restricted_memory` flag is set on group/channel tasks):

    USER.md ★    home `~/.jigga/USER.md` (per-member override) — the principal
    identity     generated from the agent config — always present
    SOUL.md      `roles/<id>/SOUL.md` — persona/voice/principles (authored)
    AGENTS.md    role charter + teammates roster (authored-or-generated)
    TEAM.md      the team charter (workspace)
    TOOLS.md     tool-usage policy (authored-or-generated from capabilities)
    MEMORY ★     role MEMORY.md + today/yesterday daily logs + pinned/recent team facts + scoped memory
    shared-ctx   lead-curated plan + priorities + recent team outputs
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jigga.core.config import load_teams
from jigga.core.models import AgentConfig
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.team_memory import read_pinned, read_team_memory
from jigga.runtime.workspaces import read_file

_LAYER_LIMIT = 2000
_TOTAL_LIMIT = 12000
_RECENT_FACTS = 8


def _clip(text: str, limit: int = _LAYER_LIMIT) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n…(truncated)"


def _home_user(home: Path) -> str | None:
    path = Path(home) / "USER.md"
    return path.read_text(encoding="utf-8") if path.exists() else None


def _identity(agent: AgentConfig) -> str:
    base = f"You are **{agent.name}**. Role: {agent.role}."
    if agent.description:
        base += f"\n{agent.description}"
    return base


def _gen_agents(agent: AgentConfig, team_id: str, teams: dict[str, Any]) -> str:
    team = teams.get(team_id)
    lines = [f"You are `{agent.id}`" + (f", the {agent.role}" if agent.role else "") + "."]
    if team is not None and team.agents:
        mates = [m for m in team.agents if isinstance(m, dict) and m.get("id") and m.get("id") != agent.id]
        if mates:
            lines.append("\n## Your teammates")
            for m in mates:
                role = m.get("role")
                lines.append(f"- `{m['id']}`" + (f" — {role}" if role else ""))
    lines.append(
        "\n## Operating rules\n"
        "- Read the task and the team plan/priorities before acting.\n"
        "- Write deliverables to `shared-context/agent-outputs/` and summarize what you did.\n"
        "- Medium/high-risk actions are gated for approval by the runtime — don't try to bypass them.\n"
        "- On a handoff, the next teammate picks up from your output, so make it self-contained."
    )
    return "\n".join(lines)


def _tools_layer(home: Path, team_id: str, member: str, agent: AgentConfig,
                 registry: CapabilityRegistry) -> str:
    """The generated grant list ALWAYS ships (it's the agent's live, enforced
    allowlist — an authored file must never blind the agent to its grants);
    an authored `roles/<id>/TOOLS.md` contributes usage NOTES on top."""
    generated = _gen_tools(agent, registry)
    notes = read_file(home, team_id, f"roles/{member}/TOOLS.md")
    if notes and notes.strip():
        return generated + "\n\n### Your tool notes\n" + _clip(notes)
    return generated


def _gen_tools(agent: AgentConfig, registry: CapabilityRegistry) -> str:
    # Effective tool allowlist, filtered to actions that resolve to a capability.
    allowed = list(agent.tools or [])
    perms = agent.permissions or {}
    tools_perm = perms.get("tools") if isinstance(perms, dict) else None
    if isinstance(tools_perm, dict):
        allowed += list(tools_perm.get("allow") or [])
    lines = ["Tools available to you (the runtime enforces permissions — these are what you may call):"]
    seen: set[str] = set()
    for action in dict.fromkeys(allowed):
        cap = registry.resolve_action(action)
        if cap is None or action in seen:
            continue
        seen.add(action)
        risk = f" [risk: {cap.risk_level}]" if cap.risk_level and cap.risk_level != "low" else ""
        lines.append(f"- `{action}` — {cap.summary}{risk}")
    if len(lines) == 1:
        lines.append("- (no tools configured — work from your own reasoning and report back)")
    lines.append("\nIf you lack a tool you need, say so in your summary rather than improvising.")
    return "\n".join(lines)


def _dated_memory(home: Path, team_id: str, member: str, today: date) -> str:
    parts: list[str] = []
    for day in (today, today - timedelta(days=1)):
        text = read_file(home, team_id, f"roles/{member}/memory/{day.isoformat()}.md")
        if text and text.strip():
            parts.append(f"#### {day.isoformat()}\n{text.strip()}")
    return "\n\n".join(parts)


def _memory_layer(home: Path, team_id: str, member: str, memory_context: dict[str, Any] | None,
                  today: date) -> str:
    parts: list[str] = []
    role_mem = read_file(home, team_id, f"roles/{member}/MEMORY.md")
    if role_mem and role_mem.strip() and "(empty)" not in role_mem:
        parts.append("### Your long-term memory\n" + _clip(role_mem))
    dated = _dated_memory(home, team_id, member, today)
    if dated:
        parts.append("### Recent daily log\n" + _clip(dated))
    pinned = read_pinned(home, team_id)
    if pinned:
        parts.append("### Pinned team facts\n" + "\n".join(f"- {p.get('text', '')}" for p in pinned[-_RECENT_FACTS:]))
    facts = read_team_memory(home, team_id)
    if facts:
        parts.append("### Recent team learnings\n" + "\n".join(f"- {f.get('text', '')}" for f in facts[-_RECENT_FACTS:]))
    for inc in (memory_context or {}).get("included", []):
        if isinstance(inc, dict) and inc.get("content"):
            parts.append(f"### Memory: {Path(str(inc.get('path'))).name}\n" + _clip(str(inc["content"])))
    return "\n\n".join(parts)


def _shared_context(home: Path, team_id: str) -> str:
    parts: list[str] = []
    plan = read_file(home, team_id, "notes/plan.md")
    if plan and plan.strip():
        parts.append("### Plan (lead-curated)\n" + _clip(plan))
    prio = read_file(home, team_id, "shared-context/priorities.md")
    if prio and prio.strip():
        parts.append("### Priorities (lead-curated)\n" + _clip(prio))
    return "\n\n".join(parts)


def assemble_agent_context(
    home: Path,
    agent: AgentConfig,
    team_id: str,
    *,
    registry: CapabilityRegistry,
    memory_context: dict[str, Any] | None = None,
    restricted: bool = False,
    now: datetime | None = None,
) -> tuple[str, list[str]]:
    """Build the agent's system-prompt body from the context-pack layers. Returns
    (text, layers_loaded). `restricted=True` (group/shared session) omits the
    private USER and MEMORY layers."""
    home = Path(home)
    member = agent.id
    today = (now or datetime.now(timezone.utc)).date()
    teams = load_teams(home / "teams")

    def authored(relpath: str) -> str | None:
        text = read_file(home, team_id, relpath)
        return text if text and text.strip() else None

    # (title, body, name, private)
    candidates: list[tuple[str, str | None, str, bool]] = [
        ("About your principal", (authored(f"roles/{member}/USER.md") or _home_user(home)), "USER", True),
        ("Who you are", _identity(agent), "identity", False),
        ("Your persona", authored(f"roles/{member}/SOUL.md"), "SOUL", False),
        ("Your role on the team", authored(f"roles/{member}/AGENTS.md") or _gen_agents(agent, team_id, teams), "AGENTS", False),
        ("Team charter", authored("TEAM.md"), "TEAM", False),
        ("Your tools", _tools_layer(home, team_id, member, agent, registry), "TOOLS", False),
        ("What you know and have done", _memory_layer(home, team_id, member, memory_context, today), "MEMORY", True),
        ("Team shared context", _shared_context(home, team_id), "shared-context", False),
    ]

    sections: list[str] = []
    loaded: list[str] = []
    total = 0
    for title, body, name, private in candidates:
        if private and restricted:
            continue
        if not body or not body.strip():
            continue
        block = f"## {title}\n{_clip(body)}"
        if total + len(block) > _TOTAL_LIMIT:
            block = block[: max(0, _TOTAL_LIMIT - total)]
        sections.append(block)
        loaded.append(name)
        total += len(block)
        if total >= _TOTAL_LIMIT:
            break
    return "\n\n".join(sections), loaded

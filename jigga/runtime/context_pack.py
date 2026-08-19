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

  STABLE (byte-identical across calls → provider prefix-caching covers it):
    USER.md ★    home `~/.jigga/USER.md` (per-member override) — the principal
    identity     generated from the agent config — always present
    SOUL.md      `roles/<id>/SOUL.md` — persona/voice/principles (authored)
    AGENTS.md    role charter + teammates roster (authored-or-generated)
    TEAM.md      the team charter (workspace)
    TOOLS        compact usage policy (full tool specs already ship in the
                 API request's function schemas — never duplicated here)
    MEMORY ★     the agent's curated `roles/<id>/MEMORY.md`
  ── volatile boundary (comment marker) ──
  VOLATILE (changes per day/run — kept LAST so it can't break the cached prefix):
    recent ★     today/yesterday daily logs + pinned/recent team facts + scoped memory
    shared-ctx   lead-curated plan + priorities

Sizing follows the Hermes model (see docs research, 2026-06-04): a small fixed
resident baseline with hard per-layer caps — recall beyond it goes through the
zero-token `memory.search` tool (#96) — rather than per-message conditional
stripping, which would thrash the provider prompt cache.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jigga.core.config import load_teams
from jigga.core.models import AgentConfig
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.skills import activated_skills_layer, skills_summary_layer
from jigga.runtime.team_memory import read_pinned, read_team_memory
from jigga.runtime.workspaces import read_file

_LAYER_LIMIT = 2000  # fallback for layers without a specific cap
# Per-layer resident caps (chars; ~4 chars/token). Tightened per the Hermes
# pattern: the always-resident slice stays small, everything else is one
# `memory.search` call away. MEMORY.md's cap is enforced by nudge at >80%
# (consolidate at write time) rather than surprise mid-thought truncation.
_LAYER_LIMITS = {
    "USER": 1400,
    "identity": 600,
    "SOUL": 1500,
    "AGENTS": 1500,
    "TEAM": 1000,
    "TOOLS": 1200,
    "MEMORY": 1200,
    "inbox": 1500,
    "recent": 1200,
    "shared-context": 1000,
}
_TOTAL_LIMIT = 9000
_RECENT_FACTS = 8
# Everything above this marker is byte-stable across calls (provider prefix
# caching covers it); everything below may change per day/run.
VOLATILE_BOUNDARY = "<!-- context: stable layers above · volatile below -->"


@dataclass
class LayerStat:
    """What one layer cost, and what it lost.

    `available` vs `included` is the whole point: a layer clipped by its own cap
    and a layer dropped by the total budget both read as "present" in the old
    `layers` list, and neither says how much of the agent's memory never made it
    into the prompt.
    """

    name: str
    available: int          # characters the source actually had
    included: int           # characters that reached the prompt (0 if dropped)
    clipped: bool           # hit its own per-layer cap
    dropped: bool           # the total budget ran out before it
    volatile: bool
    private: bool


@dataclass
class ContextStats:
    """One assembly, measured. Recorded on `agent.context.assembled`.

    JIGGA's answer to ClawRecipes ticket 0272: before deciding whether graph
    memory is P0 or P2, measure what the current recency-cap + pinning strategy
    actually injects, and how much relevance it is dropping on the floor.
    """

    chars: int = 0
    tokens_est: int = 0
    stable_chars: int = 0       # above the boundary — prefix-cache eligible
    volatile_chars: int = 0     # below it — re-billed every call
    hit_total_cap: bool = False
    restricted: bool = False
    layers: list[LayerStat] = field(default_factory=list)
    pinned_available: int = 0
    pinned_included: int = 0
    facts_available: int = 0
    facts_included: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "chars": self.chars,
            "tokens_est": self.tokens_est,
            "stable_chars": self.stable_chars,
            "volatile_chars": self.volatile_chars,
            "hit_total_cap": self.hit_total_cap,
            "restricted": self.restricted,
            "pinned_available": self.pinned_available,
            "pinned_included": self.pinned_included,
            "facts_available": self.facts_available,
            "facts_included": self.facts_included,
            # Not "layers": the event already carries the loaded-layer NAMES
            # under that key, and it is an existing contract worth not breaking.
            "layer_detail": [asdict(layer) for layer in self.layers],
        }


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
    """The generated policy ALWAYS ships; an authored `roles/<id>/TOOLS.md`
    contributes usage NOTES on top — it must never blind the agent to its
    grants (the #78 failure mode)."""
    generated = _gen_tools(agent, registry)
    notes = read_file(home, team_id, f"roles/{member}/TOOLS.md")
    if notes and notes.strip():
        return generated + "\n\n### Your tool notes\n" + _clip(notes)
    return generated


def _gen_tools(agent: AgentConfig, registry: CapabilityRegistry) -> str:
    """Compact tool POLICY — never the catalog. Every granted tool already
    ships in the API request as a function schema (name + description), so
    listing them here would pay for each one twice (#86: the old per-tool list
    was 54% of a fresh install's entire context). Resident text carries only
    what the schemas don't: the count, which calls are elevated-risk, and the
    don't-improvise rule."""
    allowed = list(agent.tools or [])
    perms = agent.permissions or {}
    tools_perm = perms.get("tools") if isinstance(perms, dict) else None
    if isinstance(tools_perm, dict):
        allowed += list(tools_perm.get("allow") or [])
    resolved: list[str] = []
    risky: list[str] = []
    for action in dict.fromkeys(allowed):
        cap = registry.resolve_action(action)
        if cap is None:
            continue
        resolved.append(action)
        if cap.risk_level and cap.risk_level not in ("low", None):
            risky.append(f"`{action}` [{cap.risk_level}]")
    if not resolved:
        return ("No tools configured — work from your own reasoning and report back. "
                "If you lack a tool you need, say so in your summary rather than improvising.")
    lines = [f"You have {len(resolved)} callable tools — their names and descriptions are in "
             "your function specs. The runtime enforces permissions."]
    if risky:
        lines.append("Elevated-risk (may require approval): " + ", ".join(risky) + ".")
    lines.append("If you lack a tool you need, say so in your summary rather than improvising.")
    return "\n".join(lines)


def _dated_memory(home: Path, team_id: str, member: str, today: date) -> str:
    parts: list[str] = []
    for day in (today, today - timedelta(days=1)):
        text = read_file(home, team_id, f"roles/{member}/memory/{day.isoformat()}.md")
        if text and text.strip():
            parts.append(f"#### {day.isoformat()}\n{text.strip()}")
    return "\n\n".join(parts)


def _curated_memory(home: Path, team_id: str, member: str) -> str | None:
    """The agent's own `roles/<id>/MEMORY.md` — STABLE layer (changes only when
    the agent deliberately curates it). At >80% of its resident cap, a usage
    nudge tells the agent to consolidate at write time — the Hermes pattern —
    so the file self-limits instead of getting truncated mid-thought here."""
    role_mem = read_file(home, team_id, f"roles/{member}/MEMORY.md")
    if not role_mem or not role_mem.strip() or "(empty)" in role_mem:
        return None
    limit = _LAYER_LIMITS["MEMORY"]
    usage = len(role_mem) / limit
    if usage <= 0.8:
        return _clip(role_mem, limit)
    nudge = (f"\n\n_(MEMORY.md is at {min(int(usage * 100), 100)}% of its context budget — "
             "consolidate/prune entries before adding new ones.)_")
    # Leave room for the nudge inside the layer cap — the assembly loop clips
    # the whole block to the layer limit again, and must never cut the nudge.
    return _clip(role_mem, max(200, limit - len(nudge) - 20)) + nudge


def _recent_memory(home: Path, team_id: str, member: str, memory_context: dict[str, Any] | None,
                   today: date, counts: dict[str, int] | None = None) -> str:
    """VOLATILE memory — changes per day/run, so it lives below the cache
    boundary: dated daily logs, pinned/recent team facts, scoped includes.

    `counts`, when passed, records how many pinned facts and team learnings
    EXISTED versus how many the `_RECENT_FACTS` window let through — the
    "is recency starving relevance" question, answerable only from both numbers.
    """
    parts: list[str] = []
    dated = _dated_memory(home, team_id, member, today)
    if dated:
        parts.append("### Recent daily log\n" + _clip(dated))
    pinned = read_pinned(home, team_id)
    if counts is not None:
        counts["pinned_available"] = len(pinned)
        counts["pinned_included"] = len(pinned[-_RECENT_FACTS:])
    if pinned:
        parts.append("### Pinned team facts\n" + "\n".join(f"- {p.get('text', '')}" for p in pinned[-_RECENT_FACTS:]))
    facts = read_team_memory(home, team_id)
    if counts is not None:
        counts["facts_available"] = len(facts)
        counts["facts_included"] = len(facts[-_RECENT_FACTS:])
    if facts:
        parts.append("### Recent team learnings\n" + "\n".join(f"- {f.get('text', '')}" for f in facts[-_RECENT_FACTS:]))
    for inc in (memory_context or {}).get("included", []):
        if isinstance(inc, dict) and inc.get("content"):
            parts.append(f"### Memory: {Path(str(inc.get('path'))).name}\n" + _clip(str(inc["content"])))
    return "\n\n".join(parts)



def _inbox_layer(home: Path, team_id: str, member: str) -> str:
    """Unread mailbox messages (W6/#62) — VOLATILE + private: surfaced on wake,
    marked read by the runtime after a successful run."""
    from jigga.runtime.mailbox import render_unread, unread_messages

    return render_unread(unread_messages(home, team_id, member))

def _lanes_layer(team_id: str, teams: dict[str, Any]) -> str:
    """The team's ticket-board lane vocabulary + meanings, so agents know what
    each column means and who gates it. Empty when the team has no board."""
    from jigga.runtime.lanes import LaneError, render_lanes

    team = teams.get(team_id)
    if team is None:
        return ""
    try:
        return render_lanes(team)
    except LaneError:
        return ""


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
    task_text: str | None = None,
) -> tuple[str, list[str], ContextStats]:
    """Build the agent's system-prompt body from the context-pack layers. Returns
    (text, layers_loaded, stats). `restricted=True` (group/shared session) omits
    the private USER and MEMORY layers.

    The stats are measurement only — nothing here changes what gets injected.
    They exist because "which layers loaded" cannot answer the question that
    decides whether graph memory is worth building: how much of what the agent
    knows is being dropped by the caps before it ever reaches the model.
    """
    home = Path(home)
    member = agent.id
    today = (now or datetime.now(timezone.utc)).date()
    teams = load_teams(home / "teams")
    counts: dict[str, int] = {}

    def authored(relpath: str) -> str | None:
        text = read_file(home, team_id, relpath)
        return text if text and text.strip() else None

    # (title, body, name, private, volatile) — stable layers first, byte-stable
    # across calls; volatile layers last, below the boundary marker, so a new
    # day or a new team fact can't invalidate the provider's cached prefix.
    candidates: list[tuple[str, str | None, str, bool, bool]] = [
        ("About your principal", (authored(f"roles/{member}/USER.md") or _home_user(home)), "USER", True, False),
        ("Who you are", _identity(agent), "identity", False, False),
        ("Your persona", authored(f"roles/{member}/SOUL.md"), "SOUL", False, False),
        ("Your role on the team", authored(f"roles/{member}/AGENTS.md") or _gen_agents(agent, team_id, teams), "AGENTS", False, False),
        ("Team charter", authored("TEAM.md"), "TEAM", False, False),
        ("Your team's ticket board", _lanes_layer(team_id, teams), "lanes", False, False),
        ("Your tools", _tools_layer(home, team_id, member, agent, registry), "TOOLS", False, False),
        ("Your skills", skills_summary_layer(registry, agent), "skills", False, False),
        ("Your long-term memory", _curated_memory(home, team_id, member), "MEMORY", True, False),
        # Trigger-matched skill instructions vary per task → below the volatile
        # boundary so they can't invalidate the provider's cached prefix.
        ("Activated skill instructions", activated_skills_layer(registry, agent, task_text),
         "skills-active", False, True),
        ("Your inbox", _inbox_layer(home, team_id, member), "inbox", True, True),
        ("What you've done recently", _recent_memory(home, team_id, member, memory_context, today, counts), "recent", True, True),
        ("Team shared context", _shared_context(home, team_id), "shared-context", False, True),
    ]

    sections: list[str] = []
    loaded: list[str] = []
    stats = ContextStats(restricted=restricted)
    total = 0
    boundary_emitted = False
    budget_spent = False
    for title, body, name, private, volatile in candidates:
        if private and restricted:
            continue
        if not body or not body.strip():
            continue
        # The layer's full contribution INCLUDING its heading, so `included`
        # (which is a rendered block) is comparable to it. Counting the heading
        # on one side only made a report read "1510 of 1390 chars included".
        raw = body.strip()
        limit = _LAYER_LIMITS.get(name, _LAYER_LIMIT)
        available = len(f"## {title}\n") + len(raw)
        if budget_spent:
            # Recorded, not silently absent. The layers that lose this race are
            # the VOLATILE ones — today's log, pinned team facts, the lead's
            # shared context — because they are deliberately last. A run where
            # that happens is a run whose agent never saw what the team knows.
            stats.layers.append(LayerStat(name=name, available=available, included=0,
                                          clipped=False, dropped=True,
                                          volatile=volatile, private=private))
            continue
        if volatile and not boundary_emitted:
            sections.append(VOLATILE_BOUNDARY)
            boundary_emitted = True
        block = f"## {title}\n{_clip(body, limit)}"
        # A layer can be shortened twice: by its own cap, then by whatever is
        # left of the total. Asked as a length comparison this reads wrong for a
        # layer barely over its cap — `_clip` appends a "…(truncated)" marker,
        # so the clipped block can be LONGER than the original and report as
        # untouched. Ask the two questions directly instead.
        budget_trimmed = total + len(block) > _TOTAL_LIMIT
        if budget_trimmed:
            block = block[: max(0, _TOTAL_LIMIT - total)]
        sections.append(block)
        loaded.append(name)
        total += len(block)
        stats.layers.append(LayerStat(
            name=name, available=available, included=len(block),
            clipped=len(raw) > limit or budget_trimmed, dropped=False,
            volatile=volatile, private=private))
        if volatile:
            stats.volatile_chars += len(block)
        else:
            stats.stable_chars += len(block)
        if total >= _TOTAL_LIMIT:
            # Do not break: the remaining layers are counted as dropped above,
            # which is the number ticket 0272 exists to produce.
            budget_spent = True
            stats.hit_total_cap = True
    stats.chars = total
    stats.tokens_est = round(total / 4)  # the ~4 chars/token rule the caps assume
    stats.pinned_available = counts.get("pinned_available", 0)
    stats.pinned_included = counts.get("pinned_included", 0)
    stats.facts_available = counts.get("facts_available", 0)
    stats.facts_included = counts.get("facts_included", 0)
    return "\n\n".join(sections), loaded, stats

"""Skills — first-class surface over `skill_pack` capabilities.

A skill is a folder (manifest.yaml + instructions.md) whose action routes the
instructions through the agent's model. Packs already install, scan, and gate
like any capability; this module adds what made them second-class:

- **Trigger surfacing.** The manifest's `triggers:` list (parsed since the
  registry landed, used nowhere until now) drives progressive disclosure in
  the context pack: every granted skill gets a one-line summary; a skill whose
  trigger matches the current task gets its full instructions injected, so the
  model knows the procedure without being asked to call anything blind.
- **`jigga skills` CLI** (list / show / create) — thin verbs over the
  registry; `create` scaffolds a user-local pack that the existing first-use
  approval flow (`jigga capabilities approve`) then gates.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jigga.core.models import AgentConfig
from jigga.runtime.capabilities import CapabilityManifest, CapabilityRegistry

# Instructions injected on a trigger match are clipped so one verbose skill
# can't crowd out the rest of the context pack.
_INSTRUCTIONS_LIMIT = 4_000


def installed_skills(registry: CapabilityRegistry) -> list[CapabilityManifest]:
    return [cap for cap in registry.list() if cap.type == "skill_pack"]


def _granted_actions(agent: AgentConfig | None) -> set[str]:
    if agent is None:
        return set()
    allowed = list(agent.tools or [])
    perms = agent.permissions or {}
    tools_perm = perms.get("tools") if isinstance(perms, dict) else None
    if isinstance(tools_perm, dict):
        allowed += list(tools_perm.get("allow") or [])
    return set(allowed)


def granted_skills(registry: CapabilityRegistry, agent: AgentConfig | None) -> list[CapabilityManifest]:
    """Skills the agent may actually invoke — surfacing an unavailable skill
    would invite the improvise-a-tool failure mode the TOOLS layer guards
    against."""
    granted = _granted_actions(agent)
    return [s for s in installed_skills(registry) if any(a in granted for a in s.actions)]


def trigger_matches(skill: CapabilityManifest, text: str) -> bool:
    """Case-insensitive whole-word/phrase match of any trigger in `text`.
    Substring-only matching would fire "mail" on "mailbox" — word boundaries
    keep activation deliberate."""
    lowered = (text or "").lower()
    for trigger in skill.triggers or []:
        phrase = str(trigger).strip().lower()
        if phrase and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", lowered):
            return True
    return False


def read_instructions(skill: CapabilityManifest) -> str | None:
    if not skill.source or skill.source == "builtin":
        return None
    path = Path(skill.source).parent / (skill.instructions or "instructions.md")
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def skills_summary_layer(registry: CapabilityRegistry, agent: AgentConfig | None) -> str | None:
    """Stable layer: one line per granted skill (name — summary; triggers)."""
    skills = granted_skills(registry, agent)
    if not skills:
        return None
    lines = ["Installed skills (procedures you can run via their tool action):"]
    for skill in sorted(skills, key=lambda s: s.name):
        triggers = f" (triggers: {', '.join(skill.triggers)})" if skill.triggers else ""
        lines.append(f"- {skill.name} — {skill.summary} [{', '.join(skill.actions)}]{triggers}")
    return "\n".join(lines)


def activated_skills_layer(
    registry: CapabilityRegistry, agent: AgentConfig | None, task_text: str | None
) -> str | None:
    """Volatile layer: full instructions for skills whose trigger matches the
    task. Injected knowledge, not a command — the model still decides whether
    to use the skill's action."""
    if not task_text:
        return None
    parts: list[str] = []
    for skill in sorted(granted_skills(registry, agent), key=lambda s: s.name):
        if not trigger_matches(skill, task_text):
            continue
        instructions = read_instructions(skill)
        if instructions and instructions.strip():
            body = instructions.strip()[:_INSTRUCTIONS_LIMIT]
            parts.append(f"### Skill: {skill.name}\n{body}")
    return "\n\n".join(parts) if parts else None


# --- CLI helpers ------------------------------------------------------------


def skill_records(registry: CapabilityRegistry) -> list[dict[str, Any]]:
    return [
        {
            "name": s.name, "summary": s.summary, "actions": s.actions,
            "triggers": s.triggers, "risk_level": s.risk_level, "source": s.source,
        }
        for s in sorted(installed_skills(registry), key=lambda s: s.name)
    ]


_MANIFEST_TEMPLATE = """\
name: {name}
version: 0.1.0
type: skill_pack
summary: {summary}
actions:
  - {action}
# Words/phrases that surface this skill's full instructions into an agent's
# context when they appear in a task (whole-word match, case-insensitive).
triggers: []
risk_level: low
instructions: instructions.md
"""

_INSTRUCTIONS_TEMPLATE = """\
# {title}

You are executing the `{name}` skill. Describe here, step by step, how to do
the job — the text of this file becomes the system prompt when an agent
invokes `{action}`, and it is also injected into the agent's context when a
trigger matches the task.
"""


def create_skill(capabilities_dir: Path, name: str, *, summary: str | None = None) -> dict[str, Any]:
    """Scaffold a user-local skill pack. First use still goes through the
    normal capability approval gate — creating a skill never auto-trusts it."""
    slug = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
    if not slug:
        raise ValueError(f"Cannot derive a skill name from {name!r}")
    pack_dir = capabilities_dir / slug
    if (pack_dir / "manifest.yaml").exists():
        raise ValueError(f"Skill already exists: {pack_dir}")
    pack_dir.mkdir(parents=True, exist_ok=True)
    action = f"{slug.replace('-', '_')}.run"
    manifest = _MANIFEST_TEMPLATE.format(name=slug, summary=summary or f"{name} skill", action=action)
    (pack_dir / "manifest.yaml").write_text(manifest, encoding="utf-8")
    (pack_dir / "instructions.md").write_text(
        _INSTRUCTIONS_TEMPLATE.format(title=name, name=slug, action=action), encoding="utf-8")
    return {"name": slug, "dir": str(pack_dir), "action": action,
            "next": f"Edit {pack_dir}/instructions.md, add triggers to manifest.yaml, "
                    f"then approve it: jigga capabilities approve {pack_dir}/manifest.yaml --approve"}

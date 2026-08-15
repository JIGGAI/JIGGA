"""First-run setup / onboarding.

A fresh install always runs this (or `jigga setup` later): it asks **who the AI
is working with** and **what the install is for**, lets the installer choose a
**chief of staff vs personal assistant** and a **communication style**, then —
from those answers, nothing hardcoded — generates the principal's `USER.md` and
scaffolds the **default agent** (the catch-all / primary assistant) with all
bundled capabilities + cross-team read access and a persona reflecting the
choices.

Pure stdin/stdout via injectable `input_fn`/`print_fn` (testable, matches the
other wizards). Re-runnable: existing files aren't clobbered unless the installer
opts to overwrite the agent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from jigga.core.io import write_yaml
from jigga.core.paths import JiggaPaths
from jigga.runtime.term_select import Option, multi_select, select_one, supports_picker

# Both roles get the same powers (all capabilities + cross-team read, per the
# design); they differ in persona + how aggressively they delegate.
_ROLES = {
    "chief": {
        "id": "chief",
        "name": "Chief of Staff",
        "role": "Chief of staff — oversees and runs the teams, routes work, and reports to the principal.",
        "posture": (
            "You are the chief of staff. Default to **delegating**: route each request to the right "
            "team or agent (use team.run / task.assign), or run a team — don't do specialist work "
            "yourself. Keep an eye on every team (team.list / team.status), unblock them, and report "
            "crisply to your principal."
        ),
    },
    "assistant": {
        "id": "assistant",
        "name": "Personal Assistant",
        "role": "Personal assistant — handles the principal's requests directly, delegating bigger work to teams.",
        "posture": (
            "You are a personal assistant. Handle small, direct requests yourself; for anything that "
            "needs a team or specialist, delegate (task.assign / team.run) and track it. You can see "
            "every team's status (team.list / team.status) so you always know what's in flight."
        ),
    },
}

_STYLES = {
    "concise": "Communicate concisely and directly — lead with the decision/answer, minimal preamble.",
    "detailed": "Communicate thoroughly — give the reasoning and relevant context, not just the answer.",
    "warm": "Communicate in a warm, conversational, personable tone.",
}

# What the assistant can do, grouped for a human rather than listed as ~30 raw
# actions. Groups are keyed by *capability name* (not action) so adding an
# action to an existing capability needs no change here; a capability that
# matches no group lands in the catch-all and stays enabled, so a new bundled
# capability is never silently withheld.
#
# `default_on` is False for anything that reaches off the machine or runs
# arbitrary commands. Granting those unasked is how an install ends up with
# more authority than its owner realizes they agreed to.
_TOOL_GROUPS: list[dict[str, Any]] = [
    {"key": "files", "label": "Files", "detail": "Read and write in the folders you allow",
     "capabilities": ["filesystem"], "default_on": True},
    {"key": "memory", "label": "Memory", "detail": "Remember, recall, and summarize",
     "capabilities": ["memory-write", "memory-search", "summarization"], "default_on": True},
    {"key": "notify", "label": "Notify", "detail": "Desktop notifications and channel messages",
     "capabilities": ["notifications", "webchat", "mailbox"], "default_on": True},
    {"key": "writing", "label": "Writing", "detail": "Draft and review prose with the model",
     "capabilities": ["text-generation", "content-drafting"], "default_on": True},
    {"key": "schedule", "label": "Schedule", "detail": "Reminders, calendar, and mail lookups",
     "capabilities": ["reminders", "calendar", "email"], "default_on": True},
    {"key": "teams", "label": "Teams", "detail": "Delegate work and oversee every team",
     "capabilities": ["team-insight", "team-orchestration", "subagent-delegation", "tickets"],
     "default_on": True},
    {"key": "web", "label": "Web", "detail": "Fetch pages and search the web (leaves this machine)",
     "capabilities": ["web"], "default_on": False},
    {"key": "shell", "label": "Shell", "detail": "Run shell commands on this machine (HIGH RISK)",
     "capabilities": ["shell"], "default_on": False},
]
_CATCH_ALL = {"key": "other", "label": "Other", "detail": "Everything else bundled with JIGGA",
              "capabilities": [], "default_on": True}


def _actions_of(cap: Any) -> list[str]:
    # Runtime-only actions (e.g. webchat.poll_messages) belong to the ingest
    # pipeline — never list them as agent tools; the dispatcher would deny them
    # and the model would just waste a turn trying.
    return [a for a in cap.actions if not cap.is_runtime_only(a)]


def _tool_groups() -> list[dict[str, Any]]:
    """The tool groups with their live action lists resolved from the bundled
    registry. Groups whose capabilities aren't installed drop out; anything
    bundled that no group claims joins the catch-all."""
    from jigga.runtime.capabilities import bundled_capabilities

    by_name = {cap.name: cap for cap in bundled_capabilities()}
    claimed: set[str] = set()
    groups: list[dict[str, Any]] = []
    for spec in _TOOL_GROUPS:
        actions: list[str] = []
        for cap_name in spec["capabilities"]:
            cap = by_name.get(cap_name)
            if cap is None:
                continue
            claimed.add(cap_name)
            actions.extend(a for a in _actions_of(cap) if a not in actions)
        if actions:
            groups.append({**spec, "actions": actions})
    leftover: list[str] = []
    for name, cap in by_name.items():
        if name not in claimed:
            leftover.extend(a for a in _actions_of(cap) if a not in leftover)
    if leftover:
        groups.append({**_CATCH_ALL, "actions": leftover})
    return groups


def _all_capability_actions() -> list[str]:
    actions: list[str] = []
    for group in _tool_groups():
        actions.extend(a for a in group["actions"] if a not in actions)
    return actions


def _choose_tools(input_fn: Callable[[str], str], print_fn: Callable[..., None],
                  agent_name: str) -> tuple[list[str], list[str]]:
    """Walk the tool groups. Returns (granted actions, enabled group labels).

    Previously every bundled action was granted unconditionally, `shell.run`
    included — a fresh install handed its assistant the ability to run
    arbitrary commands without ever saying so out loud.
    """
    groups = _tool_groups()
    title = f"What should {agent_name} be able to do?"
    if supports_picker():
        print_fn("")
        picked = multi_select(title, [
            Option(label=f"{g['label']:9} {g['detail']}", selected=bool(g["default_on"]))
            for g in groups
        ])
        chosen = groups if picked is None else [groups[i] for i in picked]
    else:
        print_fn(f"\n{title}")
        for i, g in enumerate(groups, 1):
            mark = "x" if g["default_on"] else " "
            print_fn(f"  {i}. [{mark}] {g['label']:9} {g['detail']}")
        raw = input_fn("Enter to accept, or list the numbers you want (e.g. 1,2,5): ").strip()
        if not raw:
            chosen = [g for g in groups if g["default_on"]]
        else:
            wanted = {int(p) for p in raw.replace(",", " ").split() if p.strip().isdigit()}
            chosen = [g for i, g in enumerate(groups, 1) if i in wanted]
    actions: list[str] = []
    for group in chosen:
        actions.extend(a for a in group["actions"] if a not in actions)
    return actions, [g["label"] for g in chosen]


def _normalize_dirs(raw: str) -> list[str]:
    """Parse a comma-separated list of folders into recursive filesystem-allow
    globs (e.g. `~/Projects, /data` → [`~/Projects/**`, `/data/**`]). Blank →
    none. A path that already carries a glob is kept as-is."""
    out: list[str] = []
    for part in (raw or "").split(","):
        p = part.strip().rstrip("/")
        if not p:
            continue
        glob = p if any(c in p for c in "*?[") else f"{p}/**"
        if glob not in out:
            out.append(glob)
    return out


def _choose(input_fn: Callable[[str], str], print_fn: Callable[..., None],
            prompt: str, options: list[tuple[str, str]], default: str) -> str:
    if supports_picker():
        default_index = next((i for i, (key, _) in enumerate(options) if key == default), 0)
        print_fn("")
        picked = select_one(prompt, [Option(label=label, detail="(default)" if key == default else "")
                                     for key, label in options],
                            default_index=default_index)
        return options[picked][0] if picked is not None else default
    print_fn(f"\n{prompt}")
    for i, (key, label) in enumerate(options, 1):
        marker = "  (default)" if key == default else ""
        print_fn(f"  {i}. {label}{marker}")
    raw = input_fn("Choose [number, Enter for default]: ").strip()
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return options[int(raw) - 1][0]
    # accept the key by name too
    for key, _ in options:
        if raw.lower() == key:
            return key
    return default


def _user_md(call_you: str, timezone: str, purpose: str) -> str:
    lines = ["# USER.md — About your principal",
             "_Who the agents work for. Injected into each agent's context (private sessions only)._\n"]
    lines.append(f"- **What to call them:** {call_you}" if call_you else "- **What to call them:**")
    lines.append(f"- **Timezone:** {timezone}" if timezone else "- **Timezone:**")
    lines.append("\n## Purpose of this install")
    lines.append(purpose if purpose else "_(general personal assistance)_")
    lines.append("\n## Context\n_(projects, preferences, constraints — build this over time)_\n")
    return "\n".join(lines)


def run_onboarding(
    paths: JiggaPaths,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
    overwrite: bool = False,
) -> dict[str, Any]:
    echo = print_fn

    def ask(prompt: str) -> str:
        return input_fn(prompt).strip()

    echo("\n— JIGGA setup —")
    echo("Tell me who I'm working with and what this is for. Press Enter to skip any question.\n")
    call_you = ask("What should your agents call you? ")
    timezone = ask("Your timezone? (e.g. US/Central) ")
    purpose = ask("What's the purpose of this install? Anything specific to focus on? ")

    role_kind = _choose(
        input_fn, echo, "Do you want a chief of staff or a personal assistant?",
        [("chief", _ROLES["chief"]["role"]), ("assistant", _ROLES["assistant"]["role"])],
        default="chief",
    )
    # The assistant's display name — id stays the stable archetype id (`chief`/
    # `assistant`) so routing/workspaces never depend on what you call it.
    base = _ROLES[role_kind]
    spec = dict(base)
    custom_name = ask(f"Name your assistant? (Enter for \"{base['name']}\") ")
    if custom_name:
        spec["name"] = custom_name
    agent_name = spec["name"]
    # Default they/them: a name doesn't imply pronouns, and the wrong guess is
    # worse than the neutral form. Written into SOUL.md so the agent refers to
    # itself the way its principal chose.
    pronouns = ask(f"Pronouns for {agent_name}? (Enter for they/them) ") or "they/them"
    style = _choose(
        input_fn, echo, "Communication style?",
        [("concise", _STYLES["concise"]), ("detailed", _STYLES["detailed"]), ("warm", _STYLES["warm"])],
        default="concise",
    )
    working_style = ask(f"\nAnything else about how {agent_name} should work with you? (Enter to skip) ")
    boundaries = ask(f"Anything {agent_name} should never do without asking you first? (Enter to skip) ")
    extra_dirs = _normalize_dirs(ask(
        "\nAny folders the assistant may read/write? (comma-separated, Enter for none) "))
    tools, tool_groups = _choose_tools(input_fn, echo, agent_name)

    # USER.md — generated from the answers, never shipped pre-filled.
    user_path = paths.home / "USER.md"
    if overwrite or not _looks_filled(user_path):
        user_path.write_text(_user_md(call_you, timezone, purpose), encoding="utf-8")

    # The default agent: all bundled capabilities + cross-team read, autonomous
    # so it can dispatch/run teams without a per-call approval prompt.
    agent_id = spec["id"]
    agent_file = paths.agents / f"{agent_id}.yaml"
    created = False
    if overwrite or not agent_file.exists():
        write_yaml(agent_file, {
            "id": agent_id,
            "name": spec["name"],
            "role": spec["role"],
            "description": purpose or f"The {base['name'].lower()} for this JIGGA install.",
            "default": True,
            "memory_scope": "task_only",
            "permission_mode": "autonomous",
            "tools": tools,
            "permissions": {
                "memory": {"scope": "task_only"},
                # Scoped to this install's actual runtime home (the only directory
                # we can assume exists — `--home`/JIGGA_HOME may move it off
                # ~/.jigga). `secrets/**` (incl. the runtime's own secrets dir)
                # stays denied. Add your own project paths by editing this agent.
                "filesystem": {"allow": [f"{paths.home}/**", *extra_dirs],
                               "deny": [".env", "id_rsa", "~/.ssh/**", "~/.aws/**", "secrets/**"]},
                "calendar": "read",
                "email": "read",
                "notifications": "send",
                "mailbox": "send",
                "delegation": "spawn_subagent",
                "network": {"mode": "ask"},
            },
        })
        created = True
        _write_persona(paths.home, agent_id, spec, _STYLES[style], purpose, call_you,
                       pronouns=pronouns, working_style=working_style, boundaries=boundaries,
                       overwrite=overwrite, fresh=True)

    echo(f"\n✓ Setup complete. Default agent: {agent_name} (`{agent_id}`).")
    echo(f"  USER.md: {user_path}")
    for line in _introduction(spec, call_you, timezone, purpose, style, pronouns,
                              tool_groups, extra_dirs, paths.home):
        echo(line)
    return {"agent_id": agent_id, "name": agent_name, "role_kind": role_kind,
            "style": style, "created": created, "user_md": str(user_path),
            "extra_dirs": extra_dirs, "pronouns": pronouns, "tools": tools,
            "tool_groups": tool_groups}


def _introduction(spec: dict, call_you: str, timezone: str, purpose: str, style: str,
                  pronouns: str, tool_groups: list[str], extra_dirs: list[str],
                  home: Path) -> list[str]:
    """The assistant's first words to the person it works for.

    Setup used to end on `✓ Setup complete.` — the installer had just named a
    colleague, chosen how it speaks and what it may touch, and then never heard
    from it. This is deterministic text assembled from the answers, so it works
    with no model configured and in non-interactive runs; a model-generated
    greeting replaces it when a provider is live, with this as the fallback.
    """
    name = spec["name"]
    greeting = f"Hi {call_you} — I'm {name}." if call_you else f"I'm {name}."
    lines = ["", f"— Meet {name} —", "", greeting, ""]
    lines.append(f"  {spec['role']}")
    if pronouns and pronouns != "they/them":
        lines.append(f"  Pronouns: {pronouns}")
    if purpose:
        lines.append(f"  What you've pointed me at: {purpose}")
    if timezone:
        lines.append(f"  I'll assume you're in {timezone}.")
    lines.append(f"  How I'll talk: {_STYLES[style].split('—')[0].strip().lower()}")
    lines.append("")
    if tool_groups:
        lines.append(f"  I can: {', '.join(tool_groups).lower()}")
    else:
        lines.append("  I have no tools enabled yet — I can only talk.")
    lines.append(f"  I can read and write in {home}"
                 + (f" and {', '.join(extra_dirs)}" if extra_dirs else ""))
    lines.append("  Everything I do lands in the audit log — `jigga trace` shows any of it.")
    lines.append("")
    lines.append("  Change any of this later: `jigga setup --overwrite`,")
    lines.append("  or edit my SOUL.md to change who I am.")
    lines.append("")
    return lines


def _looks_filled(user_path: Path) -> bool:
    """A USER.md that's been filled in (has a `What to call them:` value or a
    real purpose) — don't clobber it. A bare template counts as not-filled."""
    if not user_path.exists():
        return False
    text = user_path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("- **What to call them:**") and line.split(":**", 1)[-1].strip():
            return True
    return False


def _write_persona(home: Path, agent_id: str, spec: dict, style_line: str,
                   purpose: str, call_you: str, *, pronouns: str = "they/them",
                   working_style: str = "", boundaries: str = "",
                   overwrite: bool = False, fresh: bool = False) -> None:
    """Author the default agent's identity files in its workspace — SOUL.md
    (persona), AGENTS.md (charter + guardrails), and MEMORY.md (the agent's own
    curated notes) — so the context pack injects them. No TOOLS.md: the tool
    layer is generated live from the agent's yaml grants (an authored file
    would drift; a hand-created TOOLS.md still contributes usage notes via the
    context pack). Generated from the setup choices; create-only (the
    installer's edits are theirs) unless `overwrite`.

    `fresh` means the agent was just created in this same setup run: the
    workspace scaffold has only just seeded its generic SOUL/MEMORY starters
    (no human edits can exist yet), so the authored persona must replace
    them — without it, the wizard's posture/style/name answers were silently
    discarded in favor of the generic starter text."""
    from jigga.core.config import load_agents
    from jigga.core.io import ensure_dir
    from jigga.runtime.workspaces import ensure_agent_workspace, workspace_dir

    agent = load_agents(home / "agents").get(agent_id)
    if agent is None:
        return
    ws_id = ensure_agent_workspace(home, home / "teams", agent)
    role_dir = workspace_dir(home, ws_id) / "roles" / agent_id
    ensure_dir(role_dir)
    # SOUL is who the agent *is* — name, pronouns, voice, and the lines it
    # doesn't cross. It's injected on every wake, so it stays short: the
    # charter (AGENTS.md) carries procedure, this carries identity.
    soul_lines = [f"# SOUL — {spec['name']}", "", f"Your name is {spec['name']}."]
    if pronouns:
        soul_lines.append(f"Your pronouns are {pronouns}.")
    if call_you:
        soul_lines.append(f"You work for {call_you}. Address them as {call_you}.")
    soul_lines += ["", "## Your role", spec["posture"], "", "## Your voice", style_line]
    if working_style:
        soul_lines += ["", "## How your principal wants to work with you", working_style]
    if boundaries:
        soul_lines += [
            "", "## Never without asking first",
            boundaries,
            "",
            "When one of these comes up, stop and ask — an approval you didn't need "
            "costs a message; one you skipped can't be taken back.",
        ]
    soul = "\n".join(soul_lines) + "\n"
    charter = [f"# {spec['name']} — charter", "", spec["role"], ""]
    if purpose:
        charter += [f"**Purpose of this install:** {purpose}", ""]
    charter += [
        "## How you operate",
        "- You can see every team: `team.list`, `team.status`.",
        "- You dispatch work: `task.assign` (to any agent) and `team.run`.",
        "- Commands go through the task queue + audit log — keep them auditable.",
        "",
        "## Guardrails (read → act → write)",
        "Before you act:",
        "- Read the task fully; check your MEMORY.md for relevant context.",
        "- Working with a team? Read its `notes/plan.md` and `notes/status.md` first.",
        "After you act:",
        "- Put outputs where the task asks (else `shared-context/agent-outputs/`).",
        "- Record durable lessons in your MEMORY.md; keep work small and reversible.",
    ]
    memory = [
        f"# MEMORY — {spec['name']}",
        "",
        "_Curate your durable notes here: stable facts about your principal,",
        "decisions with lasting consequences, lessons learned. Keep it short and",
        "current — prune what stops being true. This file is injected into your",
        "context every wake; the structured memory system records everything else._",
    ]
    files = {
        "SOUL.md": soul,
        "AGENTS.md": "\n".join(charter) + "\n",
        "MEMORY.md": "\n".join(memory) + "\n",
    }
    for name, content in files.items():
        path = role_dir / name
        if overwrite or fresh or not path.exists():
            path.write_text(content, encoding="utf-8")

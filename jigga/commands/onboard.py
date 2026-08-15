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
from jigga.runtime.term_select import Option, select_one, supports_picker

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

# Capabilities no wizard may ever grant. Command-line access is deliberately
# unreachable from a prompt: turning it on takes a deliberate hand-edit of the
# agent's yaml for BOTH the tool grant and `permissions.shell`, so no one can
# arrive at unattended shell execution by pressing Enter through a setup flow.
# Excluded from every group *and* from the catch-all.
_NEVER_OFFERED = {"shell"}

# Granted to the primary assistant before any question is asked. An assistant
# that can't remember anything or tell you anything isn't one, and neither
# touches your disk, your network, or anyone else's work. Everything beyond
# this floor is asked for in plain language, one power at a time.
#
# This floor is the PRIMARY agent's alone. Every other agent — recipe roles,
# team members, subagents — starts at `tools: []` and is granted explicitly.
_TOOL_FLOOR: list[dict[str, Any]] = [
    {"key": "memory", "label": "Memory",
     "capabilities": ["memory-write", "memory-search", "summarization"]},
    {"key": "notify", "label": "Notify",
     "capabilities": ["notifications", "webchat", "mailbox"]},
]

# One question per power, in the assistant's own voice. Keyed by *capability
# name* (not action) so adding an action to an existing capability needs no
# change here. `detail` states the consequence honestly — what the answer
# actually costs — rather than selling the feature.
_TOOL_QUESTIONS: list[dict[str, Any]] = [
    {"key": "writing", "label": "Writing",
     "capabilities": ["text-generation", "content-drafting"],
     "question": "Should I be able to write things for you — drafts, posts, copy, "
                 "anything that needs words?",
     "detail": None, "default_yes": True},
    {"key": "files", "label": "Files",
     "capabilities": ["filesystem"],
     "question": "Should I be able to read and write files on this machine?",
     "detail": None, "default_yes": False},
    {"key": "schedule", "label": "Schedule",
     "capabilities": ["reminders", "calendar", "email"],
     "question": "Should I be able to see your calendar and search your mail?",
     "detail": "I can only read — I can't send mail or move anything on your calendar.",
     "default_yes": False},
    {"key": "teams", "label": "Teams",
     "capabilities": ["team-insight", "team-orchestration", "tickets"],
     "question": "Should I be able to bring in other agents — build teams, hand work "
                 "to them, and run them?",
     "detail": None, "default_yes": False},
    {"key": "helpers", "label": "Helpers",
     "capabilities": ["subagent-delegation"],
     "question": "Should I be able to spin up helper agents to work in parallel?",
     "detail": "Creating agents is a different power from directing existing ones, "
               "so it's a separate answer.",
     "default_yes": False},
    {"key": "web", "label": "Web",
     "capabilities": ["web"],
     "question": "Should I be able to look things up on the web?",
     "detail": "This is the first thing that leaves your machine.",
     "default_yes": False},
    {"key": "images", "label": "Images",
     "capabilities": ["media"],
     "question": "Should I be able to generate images?",
     "detail": "Each one is a paid call to an image provider, so this costs money "
               "per picture. Needs `jigga capabilities install image-generation`.",
     "default_yes": False},
]
# Anything bundled that neither the floor nor a question claims. Asked last and
# off by default, so a newly added capability is never silently withheld — but
# never silently granted either.
_CATCH_ALL: dict[str, Any] = {
    "key": "other", "label": "Other", "capabilities": [],
    "question": "There are a few other things I could do:", "detail": None, "default_yes": False,
}


def _actions_of(cap: Any) -> list[str]:
    # Runtime-only actions (e.g. webchat.poll_messages) belong to the ingest
    # pipeline — never list them as agent tools; the dispatcher would deny them
    # and the model would just waste a turn trying.
    return [a for a in cap.actions if not cap.is_runtime_only(a)]


def _resolve(specs: list[dict[str, Any]], by_name: dict[str, Any]) -> list[dict[str, Any]]:
    """Attach each spec's live action list, dropping specs whose capabilities
    aren't installed."""
    out: list[dict[str, Any]] = []
    for spec in specs:
        actions: list[str] = []
        for cap_name in spec["capabilities"]:
            cap = by_name.get(cap_name)
            if cap is not None:
                actions.extend(a for a in _actions_of(cap) if a not in actions)
        if actions:
            out.append({**spec, "actions": actions})
    return out


def _tool_groups() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(floor, questions)` with live action lists resolved from the bundled
    registry. Anything bundled that neither claims becomes a trailing catch-all
    question, so a new capability is never silently withheld."""
    from jigga.runtime.capabilities import bundled_capabilities

    by_name = {cap.name: cap for cap in bundled_capabilities() if cap.name not in _NEVER_OFFERED}
    floor = _resolve(_TOOL_FLOOR, by_name)
    questions = _resolve(_TOOL_QUESTIONS, by_name)
    claimed = {c for spec in (*_TOOL_FLOOR, *_TOOL_QUESTIONS) for c in spec["capabilities"]}
    leftover: list[str] = []
    for name, cap in by_name.items():
        if name not in claimed:
            leftover.extend(a for a in _actions_of(cap) if a not in leftover)
    if leftover:
        questions = [*questions, {**_CATCH_ALL, "actions": leftover}]
    return floor, questions


def _all_capability_actions() -> list[str]:
    floor, questions = _tool_groups()
    actions: list[str] = []
    for group in (*floor, *questions):
        actions.extend(a for a in group["actions"] if a not in actions)
    return actions


def _yes_no(input_fn: Callable[[str], str], prompt: str, *, default_yes: bool) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    raw = input_fn(f"{prompt} {suffix} ").strip().lower()
    if not raw:
        return default_yes
    return raw[0] == "y"


def _ask_tools(input_fn: Callable[[str], str], print_fn: Callable[..., None],
               agent_name: str) -> tuple[list[str], list[str], list[str]]:
    """Ask about each power in plain language. Returns
    `(granted actions, enabled labels, extra filesystem dirs)`.

    A checkbox grid asks someone to audit a list of capability names they've
    never seen. A question asks them something they already have an opinion
    about, and states what saying yes costs. The floor (memory + notifications)
    isn't asked at all — it's the minimum for an assistant to be one — and
    `shell` isn't offered at any answer.
    """
    floor, questions = _tool_groups()
    actions: list[str] = []
    labels: list[str] = []
    for group in floor:
        actions.extend(a for a in group["actions"] if a not in actions)
        labels.append(group["label"])
    print_fn(f"\nA few things about what {agent_name} can do. "
             f"Everything here is off unless you say otherwise, and can be changed later.")
    print_fn(f"({agent_name} can always remember things and send you messages — that's the floor.)")
    extra_dirs: list[str] = []
    for group in questions:
        if group["key"] == "other":
            print_fn(f"\n{group['question']} {', '.join(group['actions'])}")
            enabled = _yes_no(input_fn, "Enable those?", default_yes=False)
        else:
            print_fn("")
            if group["detail"]:
                print_fn(f"  {group['detail']}")
            enabled = _yes_no(input_fn, group["question"], default_yes=bool(group["default_yes"]))
        if not enabled:
            continue
        actions.extend(a for a in group["actions"] if a not in actions)
        labels.append(group["label"])
        # Only worth asking which folders once there's a filesystem grant to
        # scope — asked unconditionally it grants a path to nothing.
        if group["key"] == "files":
            extra_dirs = _normalize_dirs(input_fn(
                "  Which folders? I'll have no access outside them "
                "(comma-separated, Enter for just my own home) ").strip())
    return actions, labels, extra_dirs


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
    greet: bool = True,
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
    tools, tool_groups, extra_dirs = _ask_tools(input_fn, echo, agent_name)

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
    intro = _introduction(spec, call_you, timezone, purpose, style, pronouns,
                          tool_groups, extra_dirs, paths.home)
    # `jigga setup` greets here because it may be all the person runs. The
    # `onboard` chain suppresses it and greets at the very end instead, once a
    # model exists to speak for itself and the accounts are connected.
    if greet:
        for line in intro:
            echo(line)
    return {"agent_id": agent_id, "name": agent_name, "role_kind": role_kind,
            "introduction": intro, "call_you": call_you, "timezone": timezone,
            "purpose": purpose, "pronouns": pronouns, "style": style,
            "created": created, "user_md": str(user_path),
            "extra_dirs": extra_dirs, "tools": tools, "tool_groups": tool_groups}


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


def model_greeting(paths: JiggaPaths, setup: dict[str, Any]) -> str | None:
    """Have the assistant introduce itself in its own configured voice.

    The templated introduction says the right things but says them the same way
    for everyone. This hands the model the persona the installer just authored
    — its SOUL, who it works for, what it was granted — and lets it speak.

    Returns None whenever that isn't possible: no provider, dry-run provider, a
    failed call, or an empty reply. The caller falls back to the template, which
    is why nothing here raises and why the model is never the only path to a
    greeting.
    """
    from jigga.core.config import load_agents
    from jigga.runtime.model_router import (
        ModelCallItem,
        ModelCallRequest,
        call_model,
        load_model_config,
        resolve_agent_model,
        resolve_agent_model_profile,
    )

    try:
        provider = (load_model_config(paths.home).get("defaults") or {}).get("provider")
    except Exception:  # noqa: BLE001
        return None
    # dry_run answers everything successfully with canned text — a greeting from
    # it would be a fake introduction from an assistant that can't think.
    if not provider or provider == "dry_run":
        return None
    agent = load_agents(paths.agents).get(setup.get("agent_id") or "")
    if agent is None:
        return None

    facts = [f"Your principal calls themselves: {setup.get('call_you') or 'unstated'}",
             f"Timezone: {setup.get('timezone') or 'unstated'}",
             f"What this install is for: {setup.get('purpose') or 'unstated'}",
             f"What you were granted: {', '.join(setup.get('tool_groups') or []) or 'nothing yet'}"]
    if setup.get("extra_dirs"):
        facts.append(f"Folders you may read and write: {', '.join(setup['extra_dirs'])}")
    system = (
        "You have just been set up. Introduce yourself to the person you work for, in your "
        "own voice as described by your SOUL. Six sentences at most.\n\n"
        "Say what you're for, what you can currently do, and invite them to start. Do not "
        "list your tools mechanically and do not thank them for installing you.\n\n"
        "Only state what the facts below support. If you were granted nothing, say so plainly "
        "rather than implying capability you don't have — the first thing you tell them has to "
        "be true, or nothing after it is worth much.\n\n" + "\n".join(facts)
    )
    request = ModelCallRequest(
        agent_id=agent.id, role=agent.role,
        task={"id": "onboard_greeting", "title": "introduce yourself",
              "description": "First contact with your principal."},
        items=[ModelCallItem(id="greet_system", role="system", content=system),
               ModelCallItem(id="greet_user", role="user",
                             content="Introduce yourself.")],
        model=resolve_agent_model(agent),
        model_profile=resolve_agent_model_profile(agent),
        dry_run=False,
    )
    try:
        result = call_model(paths.home, paths.logs, request)
    except Exception:  # noqa: BLE001 — a greeting must never be able to fail onboarding
        return None
    if result.status != "ok" or not (result.content or "").strip():
        return None
    return result.content.strip()


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

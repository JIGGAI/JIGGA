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


def _all_capability_actions() -> list[str]:
    from jigga.runtime.capabilities import bundled_capabilities

    actions: list[str] = []
    for cap in bundled_capabilities():
        for action in cap.actions:
            if action not in actions:
                actions.append(action)
    return actions


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
    style = _choose(
        input_fn, echo, "Communication style?",
        [("concise", _STYLES["concise"]), ("detailed", _STYLES["detailed"]), ("warm", _STYLES["warm"])],
        default="concise",
    )
    extra_dirs = _normalize_dirs(ask(
        "\nAny folders the assistant may read/write? (comma-separated, Enter for none) "))

    spec = _ROLES[role_kind]
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
            "description": purpose or f"The {spec['name'].lower()} for this JIGGA install.",
            "default": True,
            "memory_scope": "task_only",
            "permission_mode": "autonomous",
            "tools": _all_capability_actions(),
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
                "delegation": "spawn_subagent",
                "network": {"mode": "ask"},
            },
        })
        created = True
        _write_persona(paths.home, agent_id, spec, _STYLES[style], purpose, call_you,
                       overwrite=overwrite)

    echo(f"\n✓ Setup complete. Default agent: {spec['name']} (`{agent_id}`).")
    echo(f"  USER.md: {user_path}")
    echo("  It's the catch-all for inbound messages and can run/oversee every team.")
    if extra_dirs:
        echo(f"  Filesystem access: its JIGGA home + {', '.join(extra_dirs)}")
    return {"agent_id": agent_id, "role_kind": role_kind, "style": style,
            "created": created, "user_md": str(user_path), "extra_dirs": extra_dirs}


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
                   purpose: str, call_you: str, *, overwrite: bool = False) -> None:
    """Author the default agent's identity files in its workspace — SOUL.md
    (persona), AGENTS.md (charter + guardrails), and MEMORY.md (the agent's own
    curated notes) — so the context pack injects them. No TOOLS.md: the tool
    layer is generated live from the agent's yaml grants (an authored file
    would drift; a hand-created TOOLS.md still contributes usage notes via the
    context pack). Generated from the setup choices; create-only (the
    installer's edits are theirs) unless `overwrite`."""
    from jigga.core.config import load_agents
    from jigga.core.io import ensure_dir
    from jigga.runtime.workspaces import ensure_agent_workspace, workspace_dir

    agent = load_agents(home / "agents").get(agent_id)
    if agent is None:
        return
    ws_id = ensure_agent_workspace(home, home / "teams", agent)
    role_dir = workspace_dir(home, ws_id) / "roles" / agent_id
    ensure_dir(role_dir)
    soul = (f"# SOUL — {spec['name']}\n{spec['posture']}\n\n{style_line}\n")
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
        if overwrite or not path.exists():
            path.write_text(content, encoding="utf-8")

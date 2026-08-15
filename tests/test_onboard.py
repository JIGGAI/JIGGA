from __future__ import annotations

from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.commands.onboard import _ROLES, run_onboarding
from jigga.core.config import load_agents, resolve_default_agent
from jigga.runtime.workspaces import ensure_agent_workspace, read_file


def _scripted(answers: list[str]):
    it = iter(answers)
    return lambda _prompt="": next(it, "")


# The wizard's questions, in the order it asks them. Tests name their answers
# instead of positioning them, so adding a question doesn't silently shift every
# other test's inputs onto the wrong prompt.
_QUESTIONS = ["call_you", "timezone", "purpose", "role", "name", "pronouns",
              "style", "working_style", "boundaries", "dirs", "tools"]


def _answers(**given: str):
    unknown = set(given) - set(_QUESTIONS)
    assert not unknown, f"unknown setup question(s): {sorted(unknown)}"
    return _scripted([given.get(q, "") for q in _QUESTIONS])


def test_onboarding_creates_default_agent_and_user_md(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    # answers: call_you, timezone, purpose, role(1=chief), name(default), style(1=concise)
    out = run_onboarding(paths, input_fn=_answers(call_you="RJ", timezone="US/Central", purpose="Run the marketing team", role="1", style="1"),
                         print_fn=lambda *a, **k: None)
    assert out["agent_id"] == "chief" and out["role_kind"] == "chief"
    assert out["name"] == "Chief of Staff"          # Enter keeps the archetype name

    # USER.md generated from the answers (nothing hardcoded)
    user = (paths.home / "USER.md").read_text()
    assert "RJ" in user and "US/Central" in user and "Run the marketing team" in user

    # default agent created, marked default, autonomous, with all caps incl. the new team caps
    agent = load_agents(paths.agents)["chief"]
    assert agent.default is True and agent.permission_mode == "autonomous"
    assert {"team.run", "task.assign", "team.list", "team.status"}.issubset(set(agent.tools))
    assert "spawn_subagent" in agent.tools and "memory.search" in agent.tools
    assert resolve_default_agent(paths.agents) == "chief"

    # persona (SOUL/AGENTS) authored into its workspace → context pack will inject it.
    # The AUTHORED soul must land, not the workspace scaffold's generic starter —
    # the posture and the chosen style line are the regression markers (they were
    # silently discarded when the create-only write lost to the generic seed).
    ws = ensure_agent_workspace(paths.home, paths.teams, agent)
    soul = read_file(paths.home, ws, "roles/chief/SOUL.md") or ""
    assert "Default to **delegating**" in soul          # archetype posture, not the generic seed
    assert "Communicate concisely" in soul              # style answer (1=concise) applied


def test_onboarding_assistant_role_option(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    out = run_onboarding(paths, input_fn=_answers(call_you="Sam", role="2", style="3"),
                         print_fn=lambda *a, **k: None)
    assert out["agent_id"] == "assistant" and out["role_kind"] == "assistant" and out["style"] == "warm"
    assert load_agents(paths.agents)["assistant"].default is True


def test_onboarding_does_not_clobber_filled_user_md(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    (paths.home / "USER.md").write_text("# USER.md\n- **What to call them:** ExistingName\n", encoding="utf-8")
    run_onboarding(paths, input_fn=_answers(call_you="NewName", role="1", style="1"),
                   print_fn=lambda *a, **k: None)
    assert "ExistingName" in (paths.home / "USER.md").read_text()   # preserved
    # ...but --overwrite replaces it
    run_onboarding(paths, input_fn=_answers(call_you="NewName", role="1", style="1"),
                   print_fn=lambda *a, **k: None, overwrite=True)
    assert "NewName" in (paths.home / "USER.md").read_text()


def test_onboarding_custom_agent_name(tmp_path: Path) -> None:
    """The name step: what you type becomes the agent's display name and flows
    into its identity files; the id stays the stable archetype id so routing
    and workspaces never depend on the name."""
    paths = init_runtime(tmp_path)
    out = run_onboarding(paths, input_fn=_answers(call_you="RJ", role="1", name="Hermes", style="1"),
                         print_fn=lambda *a, **k: None)
    assert out["name"] == "Hermes"
    assert out["agent_id"] == "chief"               # id unchanged — stable references

    agent = load_agents(paths.agents)["chief"]
    assert agent.name == "Hermes"
    assert agent.default is True
    assert "chief" in agent.description.lower()     # description keeps the archetype, not "the hermes"

    ws = ensure_agent_workspace(paths.home, paths.teams, agent)
    soul = read_file(paths.home, ws, "roles/chief/SOUL.md") or ""
    charter = read_file(paths.home, ws, "roles/chief/AGENTS.md") or ""
    assert "Your name is Hermes." in soul
    assert "chief of staff" in soul.lower()         # posture still the archetype's
    assert charter.startswith("# Hermes — charter")


# --- soul depth -------------------------------------------------------------


def test_soul_carries_pronouns_voice_and_boundaries(tmp_path: Path) -> None:
    """SOUL is who the agent *is*. The setup answers about pronouns, working
    style, and hard limits have to survive into it — they're injected on every
    wake, so anything dropped here is silently never true of the agent."""
    paths = init_runtime(tmp_path)
    run_onboarding(paths, input_fn=_answers(
        call_you="RJ", role="1", name="Ada", pronouns="she/her", style="2",
        working_style="Show me options before deciding.",
        boundaries="Never email a client or spend money."),
        print_fn=lambda *a, **k: None)
    agent = load_agents(paths.agents)["chief"]
    ws = ensure_agent_workspace(paths.home, paths.teams, agent)
    soul = read_file(paths.home, ws, "roles/chief/SOUL.md") or ""
    assert "Your name is Ada." in soul
    assert "Your pronouns are she/her." in soul
    assert "You work for RJ." in soul
    assert "Show me options before deciding." in soul
    assert "Never email a client or spend money." in soul
    assert "Communicate thoroughly" in soul                 # style=2
    assert "can't be taken back" in soul                    # the ask-first framing


def test_soul_defaults_pronouns_to_they_them(tmp_path: Path) -> None:
    """A name doesn't imply pronouns and the wrong guess is worse than the
    neutral form, so the blank answer is they/them rather than nothing."""
    paths = init_runtime(tmp_path)
    out = run_onboarding(paths, input_fn=_answers(call_you="RJ", name="Ada"),
                         print_fn=lambda *a, **k: None)
    assert out["pronouns"] == "they/them"
    agent = load_agents(paths.agents)["chief"]
    ws = ensure_agent_workspace(paths.home, paths.teams, agent)
    assert "Your pronouns are they/them." in (read_file(paths.home, ws, "roles/chief/SOUL.md") or "")


def test_soul_omits_sections_the_installer_skipped(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    run_onboarding(paths, input_fn=_answers(call_you="RJ"), print_fn=lambda *a, **k: None)
    agent = load_agents(paths.agents)["chief"]
    ws = ensure_agent_workspace(paths.home, paths.teams, agent)
    soul = read_file(paths.home, ws, "roles/chief/SOUL.md") or ""
    assert "Never without asking first" not in soul
    assert "How your principal wants to work with you" not in soul


# --- tool groups ------------------------------------------------------------


def test_default_tool_selection_excludes_shell_and_web(tmp_path: Path) -> None:
    """Blanket-granting every bundled action handed a fresh install `shell.run`
    without ever saying so. Accepting the defaults must not reach off the
    machine or run arbitrary commands."""
    paths = init_runtime(tmp_path)
    out = run_onboarding(paths, input_fn=_answers(call_you="RJ"), print_fn=lambda *a, **k: None)
    tools = set(out["tools"])
    assert "shell.run" not in tools
    assert "web.fetch" not in tools and "web.search" not in tools
    # ...while everything a useful assistant needs is still on by default.
    assert {"filesystem.read_file", "filesystem.write_file", "memory.remember", "memory.search",
            "notifications.send", "team.list", "task.assign", "spawn_subagent",
            "draft_with_model", "remind.at"} <= tools
    assert set(load_agents(paths.agents)["chief"].tools) == tools


def test_tool_groups_can_be_selected_explicitly(tmp_path: Path) -> None:
    from jigga.commands.onboard import _tool_groups

    groups = [g["key"] for g in _tool_groups()]
    shell_index = groups.index("shell") + 1
    paths = init_runtime(tmp_path)
    out = run_onboarding(paths, input_fn=_answers(call_you="RJ", tools=str(shell_index)),
                         print_fn=lambda *a, **k: None)
    assert out["tools"] == ["shell.run"]
    assert out["tool_groups"] == ["Shell"]


def test_every_bundled_action_belongs_to_some_group(tmp_path: Path) -> None:
    """A capability that no group claims must land in the catch-all, not vanish
    — otherwise adding one silently withholds it from every new install."""
    from jigga.commands.onboard import _all_capability_actions, _tool_groups
    from jigga.runtime.capabilities import bundled_capabilities

    grouped = {a for g in _tool_groups() for a in g["actions"]}
    expected = {a for cap in bundled_capabilities() for a in cap.actions
                if not cap.is_runtime_only(a)}
    assert grouped == expected
    assert set(_all_capability_actions()) == expected


# --- introduction -----------------------------------------------------------


def test_setup_ends_by_introducing_the_agent(tmp_path: Path) -> None:
    """Setup used to end on '✓ Setup complete.' — the installer named a
    colleague, chose how it speaks and what it may touch, and never heard from
    it."""
    paths = init_runtime(tmp_path)
    printed: list[str] = []
    run_onboarding(paths, input_fn=_answers(
        call_you="RJ", timezone="US/Central", purpose="Run the shop's marketing",
        role="1", name="Ada", style="1", dirs="~/Projects"),
        print_fn=lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
    out = "\n".join(printed)
    assert "— Meet Ada —" in out
    assert "Hi RJ — I'm Ada." in out
    assert "Run the shop's marketing" in out
    assert "US/Central" in out
    assert "I can: files" in out.lower() or "i can: files" in out.lower()
    assert "~/Projects/**" in out
    assert "jigga trace" in out                      # the audit promise
    assert "jigga setup --overwrite" in out          # how to change it


def test_introduction_is_honest_when_nothing_was_granted(tmp_path: Path) -> None:
    from jigga.commands.onboard import _introduction

    lines = _introduction(dict(_ROLES["chief"]), "RJ", "", "", "concise", "they/them",
                          [], [], tmp_path)
    assert any("no tools enabled yet" in line for line in lines)


def test_nothing_personal_shipped_in_repo() -> None:
    """Guard: no pre-filled USER.md with personal data ships in the repo."""
    import subprocess
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(["git", "ls-files", "USER.md", "**/USER.md"],
                             cwd=root, capture_output=True, text=True).stdout
    assert tracked.strip() == "", f"a USER.md is tracked in the repo: {tracked!r}"


def test_onboarding_grants_extra_directories(tmp_path: Path) -> None:
    """The setup 'which folders?' answer is added to the default agent's
    filesystem allowlist (as recursive globs), alongside its JIGGA home."""
    from jigga.runtime.policy import evaluate_filesystem
    paths = init_runtime(tmp_path)
    # answers: call_you, tz, purpose, role(1), name(default), style(1), dirs
    run_onboarding(paths, input_fn=_answers(
        call_you="RJ", role="1", style="1", dirs="~/Projects/site, /data/reports"),
        print_fn=lambda *a, **k: None)
    agent = load_agents(paths.agents)["chief"]
    allow = agent.permissions["filesystem"]["allow"]
    assert "~/Projects/site/**" in allow and "/data/reports/**" in allow
    assert evaluate_filesystem(agent, "~/Projects/site/index.md", "write").status == "allow"
    assert evaluate_filesystem(agent, "/data/reports/q1.csv", "read").status == "allow"
    assert evaluate_filesystem(agent, "~/other/secret.txt").status != "allow"   # not granted


def test_onboarding_authors_identity_files_create_only(tmp_path: Path) -> None:
    """The primary agent gets SOUL/AGENTS/MEMORY at setup (no TOOLS.md — the
    tool layer is generated live from yaml grants); re-running setup never
    clobbers the installer's edits."""
    paths = init_runtime(tmp_path)
    run_onboarding(paths, input_fn=_answers(call_you="RJ"),
                   print_fn=lambda *a, **k: None)
    agent = load_agents(paths.agents)["chief"]
    ws = ensure_agent_workspace(paths.home, paths.teams, agent)

    soul = read_file(paths.home, ws, "roles/chief/SOUL.md")
    charter = read_file(paths.home, ws, "roles/chief/AGENTS.md")
    memory = read_file(paths.home, ws, "roles/chief/MEMORY.md")
    assert soul and charter and memory
    assert "Guardrails (read → act → write)" in charter
    assert "MEMORY.md" in charter                              # guardrails point at the curated file
    assert "Curate your durable notes" in memory
    assert read_file(paths.home, ws, "roles/chief/TOOLS.md") is None

    # create-only: a hand-edit survives a re-run (overwrite stays False)
    from jigga.runtime.workspaces import workspace_dir
    (workspace_dir(paths.home, ws) / "roles" / "chief" / "MEMORY.md").write_text("MINE", encoding="utf-8")
    from jigga.commands.onboard import _write_persona
    _write_persona(paths.home, "chief",
                   {"name": "Chief of Staff", "posture": "p", "role": "r"}, "s", "", "")
    assert read_file(paths.home, ws, "roles/chief/MEMORY.md") == "MINE"
    _write_persona(paths.home, "chief",
                   {"name": "Chief of Staff", "posture": "p", "role": "r"}, "s", "", "",
                   overwrite=True)
    assert "Curate your durable notes" in (read_file(paths.home, ws, "roles/chief/MEMORY.md") or "")

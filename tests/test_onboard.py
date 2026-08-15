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
#
# `dirs` is conditional — only asked when `files` is answered yes — so tests
# that decline Files use `_answers_no_dirs`, which leaves it out of the
# sequence entirely rather than letting it shift every later answer by one.
_QUESTIONS = ["call_you", "timezone", "purpose", "role", "name", "pronouns",
              "style", "working_style", "boundaries",
              "writing", "files", "dirs", "schedule", "teams", "helpers", "web", "images"]


def _answers(**given: str):
    unknown = set(given) - set(_QUESTIONS)
    assert not unknown, f"unknown setup question(s): {sorted(unknown)}"
    return _scripted([given.get(q, "") for q in _QUESTIONS])


def _answers_no_dirs(**given: str):
    """Answer sequence for a run that declines Files, so `dirs` is never asked."""
    questions = [q for q in _QUESTIONS if q != "dirs"]
    unknown = set(given) - set(questions)
    assert not unknown, f"unknown setup question(s): {sorted(unknown)}"
    return _scripted([given.get(q, "") for q in questions])


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

    # default agent created, marked default, autonomous — but granted only the
    # minimal safe core until the installer asks for more (see the tool-group
    # tests below); team/filesystem powers are no longer handed over unasked.
    agent = load_agents(paths.agents)["chief"]
    assert agent.default is True and agent.permission_mode == "autonomous"
    assert {"memory.remember", "memory.search", "notifications.send"}.issubset(set(agent.tools))
    assert not {"team.run", "task.assign", "spawn_subagent"} & set(agent.tools)
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


# --- the tool questions -----------------------------------------------------


def test_pressing_enter_through_gives_the_floor_plus_writing(tmp_path: Path) -> None:
    """The floor is granted unasked; Writing is the one question defaulting yes.
    Enter through the whole flow and the assistant can remember, tell you
    things, and write — and cannot touch your disk, calendar, or the network."""
    paths = init_runtime(tmp_path)
    out = run_onboarding(paths, input_fn=_answers_no_dirs(call_you="RJ"),
                         print_fn=lambda *a, **k: None)
    tools = set(out["tools"])
    assert out["tool_groups"] == ["Memory", "Notify", "Writing"]
    assert {"memory.remember", "memory.search", "notifications.send",
            "draft_with_model"} <= tools
    for withheld in ("shell.run", "web.fetch", "web.search", "filesystem.read_file",
                     "filesystem.write_file", "calendar.list_events", "email.search",
                     "task.assign", "team.run", "spawn_subagent", "remind.at"):
        assert withheld not in tools, f"{withheld} must not be granted by default"
    assert set(load_agents(paths.agents)["chief"].tools) == tools


def test_the_floor_survives_declining_every_question(tmp_path: Path) -> None:
    """Memory and Notify are never asked — an assistant that can't remember or
    reply isn't one."""
    paths = init_runtime(tmp_path)
    out = run_onboarding(paths, input_fn=_answers_no_dirs(call_you="RJ", writing="n"),
                         print_fn=lambda *a, **k: None)
    assert out["tool_groups"] == ["Memory", "Notify"]
    assert "draft_with_model" not in out["tools"]
    assert {"memory.remember", "notifications.send"} <= set(out["tools"])


def test_each_question_grants_only_its_own_power(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    out = run_onboarding(paths, input_fn=_answers_no_dirs(
        call_you="RJ", writing="n", schedule="y"), print_fn=lambda *a, **k: None)
    tools = set(out["tools"])
    assert {"calendar.list_events", "calendar.get_event", "email.search",
            "remind.at", "remind.list"} <= tools
    assert "filesystem.read_file" not in tools and "web.fetch" not in tools
    assert "draft_with_model" not in tools


def test_helpers_is_a_separate_answer_from_teams(tmp_path: Path) -> None:
    """Creating agents is a different power from directing existing ones."""
    paths = init_runtime(tmp_path)
    out = run_onboarding(paths, input_fn=_answers_no_dirs(
        call_you="RJ", writing="n", teams="y"), print_fn=lambda *a, **k: None)
    assert {"team.run", "task.assign", "team.list"} <= set(out["tools"])
    assert "spawn_subagent" not in out["tools"]          # Helpers declined

    out = run_onboarding(paths, input_fn=_answers_no_dirs(
        call_you="RJ", writing="n", helpers="y"),
        print_fn=lambda *a, **k: None, overwrite=True)
    assert "spawn_subagent" in out["tools"]
    assert "team.run" not in out["tools"]                # Teams declined


def test_folders_are_only_asked_once_files_is_enabled(tmp_path: Path) -> None:
    """Asked unconditionally, the folders answer scopes a grant that doesn't
    exist."""
    from jigga.runtime.policy import evaluate_filesystem

    paths = init_runtime(tmp_path)
    out = run_onboarding(paths, input_fn=_answers(
        call_you="RJ", files="y", dirs="~/Projects/site, /data/reports"),
        print_fn=lambda *a, **k: None)
    assert "filesystem.write_file" in out["tools"]
    assert out["extra_dirs"] == ["~/Projects/site/**", "/data/reports/**"]
    agent = load_agents(paths.agents)["chief"]
    assert evaluate_filesystem(agent, "~/Projects/site/index.md", "write").status == "allow"
    assert evaluate_filesystem(agent, "/data/reports/q1.csv", "read").status == "allow"
    assert evaluate_filesystem(agent, "~/other/secret.txt").status != "allow"

    # Declining Files never reaches the folders question at all.
    out = run_onboarding(paths, input_fn=_answers_no_dirs(call_you="RJ", files="n"),
                         print_fn=lambda *a, **k: None, overwrite=True)
    assert out["extra_dirs"] == []


def test_shell_is_never_offered_by_the_wizard(tmp_path: Path) -> None:
    """Command-line access must be unreachable from a prompt: no question
    offers it, and it can't ride in via the catch-all either. Turning it on
    takes a deliberate hand-edit of the agent yaml."""
    from jigga.commands.onboard import _all_capability_actions, _tool_groups

    floor, questions = _tool_groups()
    assert "shell" not in {g["key"] for g in (*floor, *questions)}
    assert "shell.run" not in _all_capability_actions()
    # ...not even by saying yes to every single question.
    paths = init_runtime(tmp_path)
    out = run_onboarding(paths, input_fn=_answers(
        call_you="RJ", writing="y", files="y", schedule="y", teams="y", helpers="y",
        web="y", images="y"),
        print_fn=lambda *a, **k: None)
    assert "shell.run" not in out["tools"]


def test_only_the_primary_agent_gets_a_floor(tmp_path: Path) -> None:
    """The floor is the primary assistant's alone. Every other agent — recipe
    roles, team members, subagents — starts at `tools: []` and is granted
    explicitly, or it can do nothing at all."""
    from jigga.runtime.recipes import load_recipe, scaffold_team

    paths = init_runtime(tmp_path)
    run_onboarding(paths, input_fn=_answers_no_dirs(call_you="RJ"),
                   print_fn=lambda *a, **k: None)
    agents = load_agents(paths.agents)
    assert set(agents["chief"].tools)                       # the primary has its floor

    root = Path(__file__).resolve().parents[1]
    recipe = load_recipe(root / "examples" / "recipes" / "marketing-team.md")
    scaffold_team(paths.home, recipe, agents_dir=paths.agents, teams_dir=paths.teams,
                  workflows_dir=paths.workflows, team_id="mk")
    scaffolded = {a: c for a, c in load_agents(paths.agents).items() if a != "chief"}
    assert scaffolded, "the recipe should have produced team agents"
    for agent_id, cfg in scaffolded.items():
        # Whatever they hold came from the recipe's own `tools:`, never from a
        # default — and never includes the floor or anything shell-shaped.
        assert "shell.run" not in cfg.tools, agent_id
        assert "notifications.send" not in cfg.tools, agent_id
        assert "memory.remember" not in cfg.tools, agent_id


def test_a_recipe_role_declaring_no_tools_gets_none() -> None:
    """Omitting `tools:` in a recipe means *nothing*, not *unchecked*."""
    from jigga.runtime.recipes import _finalize_agent_doc

    doc = _finalize_agent_doc("nobody", {"role": "declares no tools"}, {})
    assert doc["tools"] == []
    assert doc["permissions"]["shell"] == {"mode": "deny"}


def test_every_offerable_action_is_reachable(tmp_path: Path) -> None:
    """A capability neither the floor nor a question claims must become the
    trailing catch-all question, not vanish — otherwise adding one silently
    withholds it from every new install. `shell` is excluded from both sides."""
    from jigga.commands.onboard import _NEVER_OFFERED, _all_capability_actions, _tool_groups
    from jigga.runtime.capabilities import bundled_capabilities

    floor, questions = _tool_groups()
    reachable = {a for g in (*floor, *questions) for a in g["actions"]}
    expected = {a for cap in bundled_capabilities() if cap.name not in _NEVER_OFFERED
                for a in cap.actions if not cap.is_runtime_only(a)}
    assert reachable == expected
    assert set(_all_capability_actions()) == expected

    # And saying yes to everything actually grants all of it.
    paths = init_runtime(tmp_path)
    out = run_onboarding(paths, input_fn=_answers(
        call_you="RJ", writing="y", files="y", schedule="y", teams="y", helpers="y",
        web="y", images="y"),
        print_fn=lambda *a, **k: None)
    assert set(out["tools"]) == expected


# --- introduction -----------------------------------------------------------


def test_setup_ends_by_introducing_the_agent(tmp_path: Path) -> None:
    """Setup used to end on '✓ Setup complete.' — the installer named a
    colleague, chose how it speaks and what it may touch, and never heard from
    it."""
    paths = init_runtime(tmp_path)
    printed: list[str] = []
    run_onboarding(paths, input_fn=_answers(
        call_you="RJ", timezone="US/Central", purpose="Run the shop's marketing",
        role="1", name="Ada", style="1", files="y", dirs="~/Projects"),
        print_fn=lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
    out = "\n".join(printed)
    assert "— Meet Ada —" in out
    assert "Hi RJ — I'm Ada." in out
    assert "Run the shop's marketing" in out
    assert "US/Central" in out
    # Names exactly what was granted — the floor, plus what was said yes to.
    assert "i can: memory, notify, writing, files" in out.lower()
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


# (the folders answer is covered by test_folders_are_only_asked_once_files_is_enabled,
#  which also pins that it isn't asked at all when Files is declined)


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

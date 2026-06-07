from __future__ import annotations

from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.commands.onboard import run_onboarding
from jigga.core.config import load_agents, resolve_default_agent
from jigga.runtime.workspaces import ensure_agent_workspace, read_file


def _scripted(answers: list[str]):
    it = iter(answers)
    return lambda _prompt="": next(it, "")


def test_onboarding_creates_default_agent_and_user_md(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    # answers: call_you, timezone, purpose, role(1=chief), name(default), style(1=concise)
    out = run_onboarding(paths, input_fn=_scripted(["RJ", "US/Central", "Run the marketing team", "1", "", "1"]),
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
    out = run_onboarding(paths, input_fn=_scripted(["Sam", "", "", "2", "", "3"]),
                         print_fn=lambda *a, **k: None)
    assert out["agent_id"] == "assistant" and out["role_kind"] == "assistant" and out["style"] == "warm"
    assert load_agents(paths.agents)["assistant"].default is True


def test_onboarding_does_not_clobber_filled_user_md(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    (paths.home / "USER.md").write_text("# USER.md\n- **What to call them:** ExistingName\n", encoding="utf-8")
    run_onboarding(paths, input_fn=_scripted(["NewName", "", "", "1", "", "1"]),
                   print_fn=lambda *a, **k: None)
    assert "ExistingName" in (paths.home / "USER.md").read_text()   # preserved
    # ...but --overwrite replaces it
    run_onboarding(paths, input_fn=_scripted(["NewName", "", "", "1", "", "1"]),
                   print_fn=lambda *a, **k: None, overwrite=True)
    assert "NewName" in (paths.home / "USER.md").read_text()


def test_onboarding_custom_agent_name(tmp_path: Path) -> None:
    """The name step: what you type becomes the agent's display name and flows
    into its identity files; the id stays the stable archetype id so routing
    and workspaces never depend on the name."""
    paths = init_runtime(tmp_path)
    out = run_onboarding(paths, input_fn=_scripted(["RJ", "", "", "1", "Hermes", "1"]),
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
    run_onboarding(paths, input_fn=_scripted(
        ["RJ", "", "", "1", "", "1", "~/Projects/site, /data/reports"]),
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
    run_onboarding(paths, input_fn=_scripted(["RJ", "", "", "", ""]),
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

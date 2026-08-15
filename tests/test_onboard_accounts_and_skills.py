"""The account walk and the skills step of onboarding.

A tool grant with no account behind it is a capability that fails the first
time it's used — `web.search` granted with no provider, `calendar.list_events`
granted with no OAuth. The walk closes that gap while the person is still there
to answer, and offers nothing for a power they declined.
"""

from __future__ import annotations

from pathlib import Path

from jigga.cli import _connect_accounts, _offer_skills
from jigga.commands.init import init_runtime
from jigga.commands.install import _copy_instructions, install_capability
from jigga.runtime.capabilities import CapabilityRegistry, load_capability_manifest


def _quiet(*_a, **_k) -> None:
    return None


def _asked(monkeypatch, answers: list[str]) -> list[str]:
    """Record every `_confirm` question asked, answering from `answers`."""
    seen: list[str] = []
    it = iter(answers)

    def _fake(question: str, *, default: bool, echo=print) -> bool:
        seen.append(question)
        raw = next(it, "")
        return default if not raw else raw == "y"

    monkeypatch.setattr("jigga.cli._confirm", _fake)
    return seen


# --- the account walk -------------------------------------------------------


def test_no_accounts_offered_when_nothing_relevant_was_granted(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    seen = _asked(monkeypatch, [])
    assert _connect_accounts(paths, ["Memory", "Notify", "Writing"], echo=_quiet) == []
    assert seen == []          # not even asked — nothing they enabled needs an account


def test_schedule_offers_the_calendar_and_mail_accounts(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    seen = _asked(monkeypatch, ["n", "n", "n"])
    _connect_accounts(paths, ["Schedule"], echo=_quiet)
    asked = " ".join(seen)
    assert "google-calendar" in asked and "gog" in asked and "email-imap" in asked
    assert "searxng" not in asked          # Web wasn't granted


def test_web_offers_the_search_providers(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    seen = _asked(monkeypatch, ["n", "n"])
    _connect_accounts(paths, ["Web"], echo=_quiet)
    asked = " ".join(seen)
    assert "searxng" in asked and "brave-search" in asked
    assert "google-calendar" not in asked


def test_telegram_is_never_offered_by_the_account_walk(tmp_path: Path, monkeypatch) -> None:
    """It's a channel — the channel question already installed it. Asking twice
    in one flow reads as a bug."""
    paths = init_runtime(tmp_path)
    seen = _asked(monkeypatch, ["n"] * 8)
    _connect_accounts(paths, ["Schedule", "Web", "Notify", "Teams"], echo=_quiet)
    assert "telegram" not in " ".join(seen)


def test_declining_every_account_installs_nothing(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    _asked(monkeypatch, ["n", "n"])
    assert _connect_accounts(paths, ["Web"], echo=_quiet) == []
    assert not (paths.capabilities / "searxng" / "manifest.yaml").exists()


def test_accounts_default_to_not_connecting(tmp_path: Path, monkeypatch) -> None:
    """Pressing Enter through must not start an OAuth flow or write a config."""
    defaults: list[bool] = []

    def _fake(question: str, *, default: bool, echo=print) -> bool:
        defaults.append(default)
        return default

    monkeypatch.setattr("jigga.cli._confirm", _fake)
    paths = init_runtime(tmp_path)
    assert _connect_accounts(paths, ["Schedule", "Web"], echo=_quiet) == []
    assert defaults and not any(defaults)


# --- the skills step --------------------------------------------------------


def test_bundled_skill_is_offered_and_installs_with_its_instructions(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    seen = _asked(monkeypatch, ["y"])
    installed = _offer_skills(paths, echo=_quiet)
    assert installed == ["daily-brief"]
    assert "daily-brief" in " ".join(seen)
    pack = paths.capabilities / "daily-brief"
    assert (pack / "manifest.yaml").exists()
    # The instructions are the skill. A manifest-only install is a skill with
    # nothing to say.
    assert (pack / "instructions.md").exists()
    assert "Daily Brief" in (pack / "instructions.md").read_text()


def test_installed_skill_surfaces_to_a_granted_agent(tmp_path: Path, monkeypatch) -> None:
    from jigga.core.models import AgentConfig
    from jigga.runtime.skills import granted_skills, read_instructions, trigger_matches

    paths = init_runtime(tmp_path)
    _asked(monkeypatch, ["y"])
    _offer_skills(paths, echo=_quiet)
    registry = CapabilityRegistry.load(user_capabilities=paths.capabilities,
                                       approvals_dir=paths.policies)
    granted = AgentConfig(id="a", name="A", role="r", tools=["skill.daily_brief"])
    skills = granted_skills(registry, granted)
    assert [s.name for s in skills] == ["daily-brief"]
    assert trigger_matches(skills[0], "give me my morning brief")
    assert "Needs you" in (read_instructions(skills[0]) or "")

    # ...and stays invisible to an agent that wasn't granted it.
    assert granted_skills(registry, AgentConfig(id="b", name="B", role="r", tools=[])) == []


def test_an_already_installed_skill_is_not_re_offered(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    install_capability(paths, "daily-brief", print_fn=_quiet)
    seen = _asked(monkeypatch, [])
    assert _offer_skills(paths, echo=_quiet) == []
    assert seen == []


def test_skills_default_to_yes(tmp_path: Path, monkeypatch) -> None:
    """Unlike accounts, a skill costs nothing until it's used and reaches
    nothing on its own — so the default leans the other way."""
    defaults: list[bool] = []

    def _fake(question: str, *, default: bool, echo=print) -> bool:
        defaults.append(default)
        return False

    monkeypatch.setattr("jigga.cli._confirm", _fake)
    paths = init_runtime(tmp_path)
    _offer_skills(paths, echo=_quiet)
    assert defaults and all(defaults)


# --- instructions copying ---------------------------------------------------


def test_instructions_filename_cannot_escape_its_pack(tmp_path: Path) -> None:
    """A pack must not name `../../something` and have the installer copy a
    file from outside its own directory."""
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "manifest.yaml").write_text(
        "name: evil\nversion: 0.1.0\nsummary: s\ntype: skill_pack\n"
        "actions: [evil.run]\ninstructions: ../../../etc/passwd\n", encoding="utf-8")
    (tmp_path / "passwd").write_text("SECRET", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()

    _copy_instructions(pack / "manifest.yaml", target, print_fn=_quiet)
    # Reduced to a basename, which doesn't exist in the pack → nothing copied.
    assert list(target.iterdir()) == []


def test_a_missing_instructions_file_is_reported_not_silent(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "manifest.yaml").write_text(
        "name: gap\nversion: 0.1.0\nsummary: s\ntype: skill_pack\n"
        "actions: [gap.run]\ninstructions: instructions.md\n", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    said: list[str] = []
    _copy_instructions(pack / "manifest.yaml", target, print_fn=lambda *a: said.append(" ".join(map(str, a))))
    assert any("missing" in line for line in said)


def test_a_connector_without_instructions_is_unaffected(tmp_path: Path) -> None:
    from jigga.optional_capabilities import REGISTRY

    manifest = load_capability_manifest(REGISTRY["searxng"].manifest_path)
    assert manifest.type != "skill_pack"
    target = tmp_path / "t"
    target.mkdir()
    _copy_instructions(REGISTRY["searxng"].manifest_path, target, print_fn=_quiet)
    assert list(target.iterdir()) == []

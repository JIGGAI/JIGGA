"""Tests for the `jigga onboard` orchestrator (cli._cmd_onboard).

Driven through `main()` to exercise arg parsing + dispatch. The interactive
model/channel wizards and the OS service install are monkeypatched so nothing
prompts or touches real launchd/systemd.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jigga.cli import main
from jigga.core.config import load_agents, resolve_default_agent


@pytest.fixture(autouse=True)
def _no_real_service(monkeypatch):
    """Defensive: no test in this file may ever install a real launchd/systemd
    service. install_service is stubbed to a harmless recorder by default; tests
    that assert on it override this with their own monkeypatch. This guards
    against a bug (or a mutated daemon gate) shelling out to the real system —
    which is exactly how an earlier mutation run installed a stray unit."""
    installs = []
    monkeypatch.setattr(
        "jigga.runtime.service.install_service",
        lambda paths, **kw: installs.append(kw) or {"backend": "systemd", "started": True, "unit_path": "/x"},
    )
    return installs


def test_onboard_non_interactive_scaffolds_defaults(tmp_path: Path) -> None:
    rc = main(["--home", str(tmp_path), "onboard", "--non-interactive",
               "--skip-model", "--skip-channels"])
    assert rc == 0
    assert (tmp_path / "USER.md").exists()
    agent_id = resolve_default_agent(tmp_path / "agents")
    assert agent_id == "chief"  # default role when not prompted
    assert load_agents(tmp_path / "agents")[agent_id].default is True


def test_onboard_install_daemon_calls_service(tmp_path: Path, monkeypatch) -> None:
    calls = {}

    def fake_install(paths, *, interval_seconds=60.0, **kw):
        calls["interval"] = interval_seconds
        calls["home"] = paths.home
        return {"backend": "systemd", "started": True, "unit_path": "/x"}

    monkeypatch.setattr("jigga.runtime.service.install_service", fake_install)

    rc = main(["--home", str(tmp_path), "onboard", "--non-interactive",
               "--skip-model", "--skip-channels", "--install-daemon", "--service-interval", "90"])
    assert rc == 0
    assert calls["interval"] == 90
    assert calls["home"] == tmp_path.resolve()


def test_onboard_without_daemon_does_not_install_service(tmp_path: Path, monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("install_service must not be called without --install-daemon")

    monkeypatch.setattr("jigga.runtime.service.install_service", boom)

    rc = main(["--home", str(tmp_path), "onboard", "--non-interactive",
               "--skip-model", "--skip-channels"])
    assert rc == 0


def test_onboard_interactive_offers_model_per_confirmation(tmp_path: Path, monkeypatch) -> None:
    # run_onboarding asks 7 questions (all blank -> defaults), then we answer
    # the model confirm "y", the channel confirm "n", and the start-now confirm "n".
    answers = iter(["", "", "", "", "", "", "", "y", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers, ""))

    ran = {"model": False, "channels": False}
    monkeypatch.setattr("jigga.cli._model_setup", lambda paths, **k: ran.update(model=True))
    monkeypatch.setattr("jigga.cli._channels_setup", lambda paths, **k: ran.update(channels=True))

    rc = main(["--home", str(tmp_path), "onboard"])
    assert rc == 0
    assert ran["model"] is True   # confirmed with "y"
    assert ran["channels"] is False  # declined with "n"


def test_onboard_unsupported_service_does_not_crash(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "jigga.runtime.service.install_service",
        lambda paths, **k: {"backend": "unsupported", "instructions": "run it yourself"},
    )
    rc = main(["--home", str(tmp_path), "onboard", "--non-interactive",
               "--skip-model", "--skip-channels", "--install-daemon"])
    assert rc == 0  # onboarding still completes; service step degrades gracefully


# ---- stdin hardening (regression: terminal focus-report escapes ate the answer) ----

def test_sanitize_answer_strips_escape_sequences():
    from jigga.cli import _sanitize_answer
    # focus-out/in events a terminal injects on alt-tab, prepended to a real "y"
    assert _sanitize_answer("\x1b[O\x1b[I\x1b[O\x1b[Iy") == "y"
    assert _sanitize_answer("  YES \n") == "yes"
    assert _sanitize_answer("\x1b[O\x1b[I") == ""  # garbage-only -> empty


def test_confirm_survives_focus_escape_noise(monkeypatch):
    from jigga.cli import _confirm
    # The exact failure RJ hit: device-login wait left focus escapes in the buffer,
    # and the channel confirm read them prepended to "y" -> wrongly treated as not-yes.
    monkeypatch.setattr("builtins.input", lambda _p="": "\x1b[O\x1b[I\x1b[O\x1b[Iy")
    assert _confirm("?", default=False) is True
    # garbage-only falls back to the default (not a spurious yes/no)
    monkeypatch.setattr("builtins.input", lambda _p="": "\x1b[O\x1b[I")
    assert _confirm("?", default=False) is False
    assert _confirm("?", default=True) is True


def test_onboard_interactive_start_now_installs(tmp_path: Path, monkeypatch, _no_real_service) -> None:
    # 16 setup blanks, model "n", channel "n" (=terminal only), search "n", start-now "y"
    answers = iter([*[""] * 16, "n", "n", "n", "y"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers, ""))
    assert main(["--home", str(tmp_path), "onboard"]) == 0
    assert len(_no_real_service) == 1  # service installed on "y"


def test_onboard_interactive_decline_start_now(tmp_path: Path, monkeypatch, _no_real_service) -> None:
    answers = iter([*[""] * 16, "n", "n", "n", "n"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers, ""))
    assert main(["--home", str(tmp_path), "onboard"]) == 0
    assert _no_real_service == []  # declined -> not installed


# --- example-recipe selection (--examples) ----------------------------------


def test_examples_setup_installs_only_the_selection(tmp_path: Path) -> None:
    from jigga.cli import _examples_setup
    from jigga.commands.init import init_runtime

    paths = init_runtime(tmp_path)
    installed = _examples_setup(paths, interactive=True, echo=lambda *a, **k: None,
                                prompt=lambda _p: "personal_admin_team")
    assert installed == ["personal_admin_team"]
    agents = load_agents(paths.agents)
    assert "daily_briefing_agent" in agents
    assert "marketing_lead" not in agents                      # unselected recipe not installed
    assert not (paths.teams / "marketing_team.yaml").exists()


def test_examples_setup_by_number_and_all_and_none(tmp_path: Path) -> None:
    from jigga.cli import _examples_setup
    from jigga.commands.init import init_runtime
    from jigga.runtime.recipes import list_recipes

    paths = init_runtime(tmp_path)
    # Enter → none installed
    assert _examples_setup(paths, interactive=True, echo=lambda *a, **k: None,
                           prompt=lambda _p: "") == []
    assert not load_agents(paths.agents)

    # "2" → exactly the second listed recipe
    second = list_recipes(paths.home)[1]["id"]
    assert _examples_setup(paths, interactive=True, echo=lambda *a, **k: None,
                           prompt=lambda _p: "2") == [second]

    # "all" → everything in the folder (deduped against ids already returned)
    installed = _examples_setup(paths, interactive=True, echo=lambda *a, **k: None,
                                prompt=lambda _p: "all")
    assert set(installed) == {r["id"] for r in list_recipes(paths.home)}


def test_examples_setup_unknown_token_is_skipped_with_notice(tmp_path: Path) -> None:
    from jigga.cli import _examples_setup
    from jigga.commands.init import init_runtime

    paths = init_runtime(tmp_path)
    notices: list[str] = []
    installed = _examples_setup(paths, interactive=True, echo=lambda msg="", *a, **k: notices.append(str(msg)),
                                prompt=lambda _p: "researcher, nope")
    assert installed == ["researcher"]
    assert any("nope" in n for n in notices)


def test_onboard_examples_non_interactive_installs_all(tmp_path: Path) -> None:
    rc = main(["--home", str(tmp_path), "onboard", "--non-interactive", "--examples",
               "--skip-model", "--skip-channels"])
    assert rc == 0
    agents = load_agents(tmp_path / "agents")
    assert {"daily_briefing_agent", "marketing_lead", "content_strategist", "researcher"} <= set(agents)


def test_onboard_examples_interactive_prompts_selection(tmp_path: Path, monkeypatch) -> None:
    # First answer feeds the examples picker; everything after defaults ("") —
    # setup wizard defaults, model/channel confirms, start-now (service stubbed).
    answers = iter(["personal_admin_team"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers, ""))
    monkeypatch.setattr("jigga.cli._model_setup", lambda paths, **k: None)
    monkeypatch.setattr("jigga.cli._channels_setup", lambda paths, **k: None)
    rc = main(["--home", str(tmp_path), "onboard", "--examples"])
    assert rc == 0
    agents = load_agents(tmp_path / "agents")
    assert "daily_briefing_agent" in agents
    assert "marketing_lead" not in agents


def test_examples_setup_uses_picker_on_a_real_terminal(tmp_path: Path, monkeypatch) -> None:
    """On a TTY the arrow-key picker drives selection; the typed prompt is
    never consulted."""
    from jigga.cli import _examples_setup
    from jigga.commands.init import init_runtime
    from jigga.runtime.recipes import list_recipes

    paths = init_runtime(tmp_path)
    target = next(i for i, r in enumerate(list_recipes(paths.home))
                  if r["id"] == "personal_admin_team")
    monkeypatch.setattr("jigga.cli.supports_picker", lambda *a, **k: True)
    monkeypatch.setattr("jigga.cli.multi_select", lambda title, options, **k: [target])
    boom = lambda _p: (_ for _ in ()).throw(AssertionError("typed prompt must not run"))  # noqa: E731

    installed = _examples_setup(paths, interactive=True, echo=lambda *a, **k: None, prompt=boom)
    assert installed == ["personal_admin_team"]
    assert "daily_briefing_agent" in load_agents(paths.agents)


def test_examples_setup_picker_cancel_installs_nothing(tmp_path: Path, monkeypatch) -> None:
    from jigga.cli import _examples_setup
    from jigga.commands.init import init_runtime

    paths = init_runtime(tmp_path)
    monkeypatch.setattr("jigga.cli.supports_picker", lambda *a, **k: True)
    monkeypatch.setattr("jigga.cli.multi_select", lambda title, options, **k: None)
    assert _examples_setup(paths, interactive=True, echo=lambda *a, **k: None) == []
    assert not load_agents(paths.agents)


def test_examples_setup_falls_back_to_typed_prompt_without_tty(tmp_path: Path, monkeypatch) -> None:
    """No TTY (pipes, tests) → the typed numbers/names prompt still works."""
    from jigga.cli import _examples_setup
    from jigga.commands.init import init_runtime

    paths = init_runtime(tmp_path)
    monkeypatch.setattr("jigga.cli.supports_picker", lambda *a, **k: False)
    installed = _examples_setup(paths, interactive=True, echo=lambda *a, **k: None,
                                prompt=lambda _p: "personal_admin_team")
    assert installed == ["personal_admin_team"]

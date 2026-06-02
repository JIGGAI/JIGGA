"""Tests for the `jigga onboard` orchestrator (cli._cmd_onboard).

Driven through `main()` to exercise arg parsing + dispatch. The interactive
model/channel wizards and the OS service install are monkeypatched so nothing
prompts or touches real launchd/systemd.
"""

from __future__ import annotations

from pathlib import Path

from jigga.cli import main
from jigga.core.config import load_agents, resolve_default_agent


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
    # run_onboarding asks 6 questions (all blank -> defaults), then we answer
    # the model confirm "y" and the channel confirm "n".
    answers = iter(["", "", "", "", "", "", "y", "n"])
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

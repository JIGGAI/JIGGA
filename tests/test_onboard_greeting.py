"""The assistant's first words, spoken by the model when there is one.

The templated introduction says the right things but says them identically for
everyone. `model_greeting` hands the model the persona the installer just
authored and lets it speak — and returns None whenever it can't, because the
template must remain the floor rather than an error path.
"""

from __future__ import annotations

from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.commands.onboard import model_greeting, run_onboarding
from jigga.core.io import write_yaml

_QUESTIONS = ["call_you", "timezone", "purpose", "role", "name", "pronouns",
              "style", "working_style", "boundaries",
              "writing", "files", "schedule", "teams", "helpers", "web"]


def _answers(**given: str):
    it = iter([given.get(q, "") for q in _QUESTIONS])
    return lambda _prompt="": next(it, "")


def _setup(tmp_path: Path, **given: str) -> tuple:
    paths = init_runtime(tmp_path)
    result = run_onboarding(paths, input_fn=_answers(**given),
                            print_fn=lambda *a, **k: None, greet=False)
    return paths, result


def _use_provider(paths, provider: str = "openai") -> None:
    write_yaml(paths.config, {"models": {"defaults": {"provider": provider}}})


# --- when the model can't speak ---------------------------------------------


def test_no_provider_means_no_model_greeting(tmp_path: Path) -> None:
    paths, setup = _setup(tmp_path, call_you="RJ")
    write_yaml(paths.config, {"models": {}})
    assert model_greeting(paths, setup) is None


def test_dry_run_provider_never_speaks(tmp_path: Path, monkeypatch) -> None:
    """dry_run answers everything successfully with canned text. A greeting
    from it would be a fake introduction from an assistant that can't think."""
    paths, setup = _setup(tmp_path, call_you="RJ")     # init leaves provider: dry_run
    monkeypatch.setattr("jigga.runtime.model_router.call_model",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    assert model_greeting(paths, setup) is None


def test_a_failed_call_falls_back_rather_than_raising(tmp_path: Path, monkeypatch) -> None:
    paths, setup = _setup(tmp_path, call_you="RJ")
    _use_provider(paths)

    def _dead(*_a, **_k):
        raise RuntimeError("OAuth token refresh failed")

    monkeypatch.setattr("jigga.runtime.model_router.call_model", _dead)
    assert model_greeting(paths, setup) is None


def test_an_empty_reply_is_not_a_greeting(tmp_path: Path, monkeypatch) -> None:
    paths, setup = _setup(tmp_path, call_you="RJ")
    _use_provider(paths)
    monkeypatch.setattr(
        "jigga.runtime.model_router.call_model",
        lambda *a, **k: type("R", (), {"status": "ok", "content": "   ", "error": None})())
    assert model_greeting(paths, setup) is None


def test_a_non_ok_result_is_not_a_greeting(tmp_path: Path, monkeypatch) -> None:
    paths, setup = _setup(tmp_path, call_you="RJ")
    _use_provider(paths)
    monkeypatch.setattr(
        "jigga.runtime.model_router.call_model",
        lambda *a, **k: type("R", (), {"status": "error", "content": "hi", "error": "budget"})())
    assert model_greeting(paths, setup) is None


# --- when it can -------------------------------------------------------------


def test_the_model_is_given_the_persona_and_the_facts(tmp_path: Path, monkeypatch) -> None:
    paths, setup = _setup(tmp_path, call_you="RJ", timezone="US/Central",
                          purpose="Run the shop's marketing", name="Ada", files="y")
    _use_provider(paths)
    seen: dict = {}

    def _capture(home, logs_dir, request):
        seen["system"] = next(i.content for i in request.items if i.role == "system")
        seen["agent_id"] = request.agent_id
        seen["dry_run"] = request.dry_run
        return type("R", (), {"status": "ok", "content": "Hi RJ — I'm Ada.", "error": None})()

    monkeypatch.setattr("jigga.runtime.model_router.call_model", _capture)
    assert model_greeting(paths, setup) == "Hi RJ — I'm Ada."
    assert seen["agent_id"] == "chief" and seen["dry_run"] is False
    system = seen["system"]
    assert "RJ" in system
    assert "US/Central" in system
    assert "Run the shop's marketing" in system
    assert "Memory, Notify, Writing" in system          # what was actually granted
    # ...and it is told not to overstate what it can do.
    assert "Only state what the facts below support" in system


def test_a_greeting_is_stripped(tmp_path: Path, monkeypatch) -> None:
    paths, setup = _setup(tmp_path, call_you="RJ")
    _use_provider(paths)
    monkeypatch.setattr(
        "jigga.runtime.model_router.call_model",
        lambda *a, **k: type("R", (), {"status": "ok", "content": "\n\n  Hello.  \n", "error": None})())
    assert model_greeting(paths, setup) == "Hello."


def test_an_assistant_granted_nothing_is_told_to_say_so(tmp_path: Path, monkeypatch) -> None:
    """The first thing it tells them has to be true, or nothing after it is
    worth much."""
    paths, setup = _setup(tmp_path, call_you="RJ", writing="n")
    _use_provider(paths)
    seen: dict = {}

    def _capture(_home, _logs, request):
        seen["system"] = next(i.content for i in request.items if i.role == "system")
        return type("R", (), {"status": "ok", "content": "hi", "error": None})()

    monkeypatch.setattr("jigga.runtime.model_router.call_model", _capture)
    model_greeting(paths, setup)
    assert "say so plainly rather than implying capability you don't have" in seen["system"]


# --- the template stays the floor -------------------------------------------


def test_setup_still_greets_from_the_template(tmp_path: Path) -> None:
    """`jigga setup` may be all someone runs, and it can't assume a model."""
    paths = init_runtime(tmp_path)
    printed: list[str] = []
    run_onboarding(paths, input_fn=_answers(call_you="RJ", name="Ada"),
                   print_fn=lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
    assert any("— Meet Ada —" in line for line in printed)


def test_greet_false_returns_the_lines_instead_of_printing_them(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    printed: list[str] = []
    result = run_onboarding(paths, input_fn=_answers(call_you="RJ", name="Ada"),
                            print_fn=lambda *a, **k: printed.append(" ".join(str(x) for x in a)),
                            greet=False)
    assert not any("— Meet Ada —" in line for line in printed)
    assert any("— Meet Ada —" in line for line in result["introduction"])

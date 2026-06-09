"""Model resilience: client-side min spacing (#83), 429 circuit breaker (#84),
and provider fallback on rate-limit (#85)."""

from __future__ import annotations

from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime import model_router, model_throttle
from jigga.runtime.model_router import ModelCallItem, ModelCallRequest, ModelCallResult, RateLimitError, call_model


# --- throttle unit ---------------------------------------------------------


def test_due_wait_tracks_last_call(tmp_path: Path):
    assert model_throttle.due_wait(tmp_path, "p", 10, now=100.0) == 0.0  # no prior call
    model_throttle.record_call(tmp_path, "p", now=100.0)
    assert model_throttle.due_wait(tmp_path, "p", 10, now=105.0) == 5.0  # 5s left in the window
    assert model_throttle.due_wait(tmp_path, "p", 10, now=111.0) == 0.0  # window elapsed
    assert model_throttle.due_wait(tmp_path, "p", 0, now=105.0) == 0.0   # spacing disabled


def test_breaker_opens_after_threshold_and_success_clears(tmp_path: Path):
    assert model_throttle.breaker_open(tmp_path, "p", now=0.0) is False
    assert model_throttle.note_rate_limited(tmp_path, "p", now=0.0, threshold=2, cooldown=60) is False
    assert model_throttle.note_rate_limited(tmp_path, "p", now=0.0, threshold=2, cooldown=60) is True
    assert model_throttle.breaker_open(tmp_path, "p", now=30.0) is True
    assert model_throttle.breaker_open(tmp_path, "p", now=61.0) is False  # cooldown elapsed
    # a success resets the streak so the next 429 starts from zero
    model_throttle.note_rate_limited(tmp_path, "p", now=70.0, threshold=2, cooldown=60)
    model_throttle.note_success(tmp_path, "p")
    assert model_throttle.note_rate_limited(tmp_path, "p", now=80.0, threshold=2, cooldown=60) is False


# --- call_model integration ------------------------------------------------


def _models(paths, models: dict) -> None:
    cfg = read_yaml(paths.config)
    cfg["models"] = models
    write_yaml(paths.config, cfg)


def _req() -> ModelCallRequest:
    return ModelCallRequest(agent_id="a", role="r", task={"id": "t", "title": "x"},
                            items=[ModelCallItem(id="s", role="system", content="x")], dry_run=False)


def test_fallback_provider_used_when_primary_rate_limited(tmp_path: Path, monkeypatch):
    paths = init_runtime(tmp_path)
    _models(paths, {
        "providers": {"chatgpt": {"kind": "chatgpt_oauth", "default_model": "gpt-5"},
                      "dry_run": {"kind": "dry_run", "default_model": "dry-run"}},
        "profiles": {"default": {"primary": "chatgpt", "fallback": ["dry_run"]}},
    })
    monkeypatch.setattr(model_router, "_call_chatgpt_oauth",
                        lambda *a, **k: (_ for _ in ()).throw(RateLimitError("429")))
    result = call_model(paths.home, paths.logs, _req())
    assert result.status == "ok" and result.provider == "dry_run" and result.fallback_used is True


def test_breaker_skips_primary_on_next_call(tmp_path: Path, monkeypatch):
    paths = init_runtime(tmp_path)
    _models(paths, {
        "providers": {"chatgpt": {"kind": "chatgpt_oauth", "default_model": "gpt-5"},
                      "dry_run": {"kind": "dry_run", "default_model": "dry-run"}},
        "profiles": {"default": {"primary": "chatgpt", "fallback": ["dry_run"]}},
        "defaults": {"rate_limit_threshold": 1, "rate_limit_cooldown_seconds": 300},
    })
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise RateLimitError("429")

    monkeypatch.setattr(model_router, "_call_chatgpt_oauth", boom)
    r1 = call_model(paths.home, paths.logs, _req())   # chatgpt 429 → breaker opens (threshold 1) → dry_run
    assert r1.provider == "dry_run" and calls["n"] == 1
    r2 = call_model(paths.home, paths.logs, _req())   # breaker open → chatgpt skipped, straight to dry_run
    assert r2.provider == "dry_run" and calls["n"] == 1  # NOT called again


def test_min_spacing_sleeps_between_calls(tmp_path: Path, monkeypatch):
    paths = init_runtime(tmp_path)
    _models(paths, {
        "providers": {"chatgpt": {"kind": "chatgpt_oauth", "default_model": "gpt-5",
                                  "min_seconds_between_calls": 10}},
        "profiles": {"default": {"primary": "chatgpt"}},
    })
    ok = ModelCallResult(status="ok", provider="chatgpt", model="gpt-5", content="hi", dry_run=False)
    monkeypatch.setattr(model_router, "_call_chatgpt_oauth", lambda *a, **k: ok)
    clock = {"t": 1000.0}
    monkeypatch.setattr(model_router.time, "time", lambda: clock["t"])
    slept: list[float] = []
    monkeypatch.setattr(model_router.time, "sleep", lambda s: slept.append(s))

    call_model(paths.home, paths.logs, _req())   # 1st: records last_call=1000, no wait
    assert slept == []
    clock["t"] = 1003.0                            # 3s later — inside the 10s window
    call_model(paths.home, paths.logs, _req())   # 2nd: must wait the remaining 7s
    assert slept == [7.0]

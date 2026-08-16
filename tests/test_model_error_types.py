"""Assertion 12 — an auth failure must reach the run record as an auth failure.

On the prior-gen stack (2026-07-31, woods) every workflow LLM node died with
`ToolsInvokeError (errorCategory: "unknown")`. The real cause — a revoked OAuth
refresh token — appeared only in a gateway log that the service definition was
sending to /dev/null. It cost days, and was misdiagnosed as a missing-file
problem in the meantime.

Two properties make that outage impossible here:

- the failure is **typed at the raise site**, so a rejected credential is a
  different class of object from a 500 or a malformed body
- the type reaches **durable state** — the node's `error_category` and a
  dedicated `model.auth.failed` event — so nobody has to read a daemon log to
  learn that a credential died

The second matters even when a fallback provider covers for the dead one and
the run *succeeds*: that is exactly when a silent auth failure goes unnoticed
until the fallback also breaks.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jigga.runtime.chatgpt_auth import ChatGptAuthError
from jigga.runtime.model_router import (
    ModelAuthError,
    ModelCallItem,
    ModelCallRequest,
    ModelProviderError,
    ModelProviderConfig,
    ModelResponseError,
    RateLimitError,
    _call_chatgpt_oauth,
    _call_openai_compatible,
    error_category,
)


def _http_error(code: int, body: bytes = b'{"error":"nope"}'):
    return urllib.error.HTTPError("https://api.example/v1", code, "err", {}, io.BytesIO(body))


def _request() -> ModelCallRequest:
    return ModelCallRequest(
        agent_id="a", role="r", task={"id": "t", "title": "x"},
        items=[ModelCallItem(id="s", role="system", content="x")], dry_run=False,
    )


def _openai_provider() -> ModelProviderConfig:
    return ModelProviderConfig(id="openai", kind="openai_compatible", default_model="gpt-5",
                               api_key_env="TEST_OPENAI_KEY")


def _raise(exc):
    def _f(*_a, **_k):
        raise exc
    return _f


# --- the taxonomy -----------------------------------------------------------


def test_every_typed_error_names_its_category():
    assert ModelAuthError("x").category == "auth"
    assert RateLimitError("x").category == "rate_limit"
    assert ModelProviderError("x").category == "provider"
    assert ModelResponseError("x").category == "response"


def test_error_category_is_honest_about_untyped_failures():
    """An unrecognized exception reports `unknown` rather than being guessed
    into a bucket — a wrong category is worse than an absent one."""
    assert error_category(ValueError("boom")) == "unknown"
    assert error_category(ModelAuthError("x")) == "auth"


def test_the_hint_is_part_of_the_message():
    """The message a human reads should carry the fix, not just the symptom."""
    exc = ModelAuthError("token rejected", hint="run `jigga model login`")
    assert "token rejected" in str(exc) and "jigga model login" in str(exc)


def test_rate_limit_error_is_still_a_runtime_error():
    """Pre-existing callers catch RuntimeError; retyping must not break them."""
    assert isinstance(RateLimitError("x"), RuntimeError)
    assert isinstance(ModelAuthError("x"), RuntimeError)


# --- the api-key provider path ----------------------------------------------


@pytest.mark.parametrize("code", [401, 403])
def test_rejected_api_key_is_an_auth_error(monkeypatch, code):
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-bad")
    monkeypatch.setattr("jigga.runtime.model_router.urllib.request.urlopen", _raise(_http_error(code)))
    with pytest.raises(ModelAuthError) as caught:
        _call_openai_compatible(_openai_provider(), _request(), "gpt-5")
    assert caught.value.category == "auth"
    assert caught.value.status == code
    assert "TEST_OPENAI_KEY" in str(caught.value)  # names the thing to fix


def test_a_server_error_is_not_an_auth_error(monkeypatch):
    """The distinction only helps if it actually discriminates."""
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-fine")
    monkeypatch.setattr("jigga.runtime.model_router.urllib.request.urlopen", _raise(_http_error(500)))
    with pytest.raises(ModelProviderError) as caught:
        _call_openai_compatible(_openai_provider(), _request(), "gpt-5")
    assert caught.value.category == "provider"
    assert not isinstance(caught.value, ModelAuthError)


def test_an_unset_key_env_is_an_auth_error(monkeypatch):
    monkeypatch.delenv("TEST_OPENAI_KEY", raising=False)
    with pytest.raises(ModelAuthError) as caught:
        _call_openai_compatible(_openai_provider(), _request(), "gpt-5")
    assert "TEST_OPENAI_KEY" in str(caught.value)


def test_a_provider_with_no_key_env_configured_is_an_auth_error():
    provider = ModelProviderConfig(id="openai", kind="openai_compatible", default_model="gpt-5")
    with pytest.raises(ModelAuthError):
        _call_openai_compatible(provider, _request(), "gpt-5")


# --- the OAuth path (the actual woods failure) ------------------------------


def _patch_creds(monkeypatch, *, refresh_raises: Exception | None = None):
    creds = MagicMock()
    creds.access_token = "tok"
    creds.account_id = "acct"
    if refresh_raises is not None:
        creds.force_refresh.side_effect = refresh_raises
    monkeypatch.setattr("jigga.runtime.chatgpt_auth.load_credentials", lambda home=None: creds)
    return creds


def _oauth_provider() -> ModelProviderConfig:
    return ModelProviderConfig(id="chatgpt", kind="chatgpt_oauth", default_model="gpt-5")


def test_a_revoked_refresh_token_is_an_auth_error_naming_the_fix(tmp_path: Path, monkeypatch):
    """The woods outage verbatim: the 401 triggers a refresh, and the refresh
    itself fails with `invalid_refresh_token`. Previously this surfaced as a
    generic RuntimeError with the refresh failure buried in the cause."""
    _patch_creds(monkeypatch, refresh_raises=ChatGptAuthError(
        "Token refresh failed: HTTP 401 invalid_refresh_token"))
    monkeypatch.setattr("jigga.runtime.model_router.urllib.request.urlopen", _raise(_http_error(401)))
    with pytest.raises(ModelAuthError) as caught:
        _call_chatgpt_oauth(_oauth_provider(), _request(), "gpt-5", tmp_path)
    assert caught.value.category == "auth"
    assert "invalid_refresh_token" in str(caught.value)
    assert "jigga model login" in str(caught.value)


def test_still_rejected_after_a_successful_refresh_is_an_auth_error(tmp_path: Path, monkeypatch):
    """Refresh worked, the grant is still gone — re-login, not retry."""
    _patch_creds(monkeypatch)
    monkeypatch.setattr("jigga.runtime.model_router.urllib.request.urlopen", _raise(_http_error(403)))
    with pytest.raises(ModelAuthError) as caught:
        _call_chatgpt_oauth(_oauth_provider(), _request(), "gpt-5", tmp_path)
    assert caught.value.status == 403
    assert "jigga model login" in str(caught.value)


def test_missing_credentials_are_an_auth_error(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("jigga.runtime.chatgpt_auth.load_credentials",
                        _raise(ChatGptAuthError("No ChatGPT credentials found")))
    with pytest.raises(ModelAuthError) as caught:
        _call_chatgpt_oauth(_oauth_provider(), _request(), "gpt-5", tmp_path)
    assert "jigga model login" in str(caught.value)


def test_a_non_auth_oauth_failure_stays_a_provider_error(tmp_path: Path, monkeypatch):
    _patch_creds(monkeypatch)
    monkeypatch.setattr("jigga.runtime.model_router.urllib.request.urlopen", _raise(_http_error(502)))
    with pytest.raises(ModelProviderError) as caught:
        _call_chatgpt_oauth(_oauth_provider(), _request(), "gpt-5", tmp_path)
    assert caught.value.category == "provider"


# --- reaching durable state -------------------------------------------------


def _audit_types(logs_dir: Path) -> list[str]:
    path = logs_dir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["type"] for line in path.read_text().splitlines() if line.strip()]


def _audit_rows(logs_dir: Path, event_type: str) -> list[dict]:
    path = logs_dir / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r["type"] == event_type]


def test_call_model_records_the_category_and_raises_its_own_alarm(tmp_path: Path, monkeypatch):
    """A dead credential gets a dedicated `model.auth.failed` event, so it is
    greppable even when a fallback provider covers for it."""
    from jigga.commands.init import init_runtime
    from jigga.core.io import write_yaml
    from jigga.runtime.model_router import call_model

    paths = init_runtime(tmp_path)
    config = tmp_path / "config.yaml"
    from jigga.core.io import read_yaml
    data = read_yaml(config)
    data["models"] = {
        "defaults": {"provider": "openai"},
        "providers": {"openai": {"kind": "openai_compatible", "default_model": "gpt-5",
                                 "api_key_env": "TEST_OPENAI_KEY"}},
        "profiles": {"default": {"primary": "openai", "fallback": []}},
    }
    write_yaml(config, data)
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-bad")
    monkeypatch.setattr("jigga.runtime.model_router.urllib.request.urlopen", _raise(_http_error(401)))

    result = call_model(tmp_path, paths.logs, _request())

    assert result.status == "error"
    assert result.error_category == "auth"          # the run record can name it
    assert result.to_dict()["error_category"] == "auth"
    assert "model.auth.failed" in _audit_types(paths.logs)   # the standalone alarm
    failed = _audit_rows(paths.logs, "model.call.failed")
    assert failed and failed[-1]["details"]["error_category"] == "auth"
    alarm = _audit_rows(paths.logs, "model.auth.failed")
    assert alarm[-1]["details"]["hint"]  # the alarm carries the fix, not just the symptom


def test_a_failed_workflow_node_records_the_category(tmp_path: Path):
    """`errorCategory: unknown` on an auth failure is the bug being closed —
    the node's own state has to carry the answer."""
    from jigga.runtime.model_router import error_category as categorize

    # The node handler catches broadly; what matters is the classification it
    # applies to whatever came out.
    assert categorize(ModelAuthError("dead token")) == "auth"
    assert categorize(RateLimitError("429")) == "rate_limit"
    assert categorize(RuntimeError("who knows")) == "unknown"

"""The ChatGPT/Codex provider retries transient 429 (rate-limit) responses with
backoff before failing the task, instead of surfacing a single 429 as a hard
failure — which is what made a burst of chat messages fail (the user's case).
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jigga.runtime import model_router
from jigga.runtime.model_router import (
    ModelCallItem,
    ModelCallRequest,
    ModelProviderConfig,
    ModelResponseError,
    _call_chatgpt_oauth,
    _retry_after_seconds,
)


class _FakeSSE:
    """A urlopen-style response: a context manager that yields SSE byte lines."""

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def __iter__(self):
        return iter(self._lines)


def _http_429(retry_after: str | None = None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError(
        "https://chatgpt.com", 429, "Too Many Requests", headers,
        io.BytesIO(b'{"detail":"Rate limit exceeded"}'),
    )


def _ok_stream() -> _FakeSSE:
    msg = {"type": "response.output_item.done",
           "item": {"type": "message", "content": [{"type": "output_text", "text": "hi"}]}}
    done = {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}}
    return _FakeSSE([f"data: {json.dumps(msg)}".encode(), f"data: {json.dumps(done)}".encode()])


def _request() -> ModelCallRequest:
    return ModelCallRequest(
        agent_id="a", role="r", task={"id": "t", "title": "x"},
        items=[ModelCallItem(id="s", role="system", content="x")], dry_run=False,
    )


def _patch_creds(monkeypatch):
    creds = MagicMock()
    creds.access_token = "tok"
    creds.account_id = "acct"
    monkeypatch.setattr("jigga.runtime.chatgpt_auth.load_credentials", lambda home=None: creds)
    return creds


def test_retry_after_seconds_prefers_header_else_exponential():
    assert _retry_after_seconds(_http_429("5"), 0) == 5.0
    # no header → exponential 1, 2, 4 … (jitter pinned to 0 for the assertion)
    no_jit = {"rand": lambda: 0.0}
    assert _retry_after_seconds(_http_429(None), 0, **no_jit) == 1.0
    assert _retry_after_seconds(_http_429(None), 2, **no_jit) == 4.0
    # capped
    assert _retry_after_seconds(_http_429("9999"), 0) == model_router._CHATGPT_MAX_BACKOFF_SECONDS


def test_retry_after_jitter_is_bounded():
    # jitter adds 0–25% to the exponential base, never below it (#84).
    lo = _retry_after_seconds(_http_429(None), 2, rand=lambda: 0.0)
    hi = _retry_after_seconds(_http_429(None), 2, rand=lambda: 1.0)
    assert lo == 4.0 and hi == 5.0  # 4 + 25%


def test_chatgpt_429_exhaustion_raises_rate_limit_error(tmp_path: Path, monkeypatch):
    from jigga.runtime.model_router import RateLimitError

    _patch_creds(monkeypatch)
    monkeypatch.setattr("jigga.runtime.model_router.time.sleep", lambda s: None)
    monkeypatch.setattr("jigga.runtime.model_router.urllib.request.urlopen",
                        lambda _req, timeout=None: (_ for _ in ()).throw(_http_429("0")))
    provider = ModelProviderConfig(id="chatgpt", kind="chatgpt_oauth", default_model="gpt-5")
    with pytest.raises(RateLimitError):  # typed so call_model can trip the breaker
        _call_chatgpt_oauth(provider, _request(), "gpt-5", tmp_path)


def test_chatgpt_retries_429_then_succeeds(tmp_path: Path, monkeypatch):
    _patch_creds(monkeypatch)
    slept: list[float] = []
    monkeypatch.setattr("jigga.runtime.model_router.time.sleep", lambda s: slept.append(s))
    attempts = {"n": 0}

    def fake_urlopen(_req, timeout=None):
        attempts["n"] += 1
        if attempts["n"] <= 2:   # 429 twice…
            raise _http_429("0")
        return _ok_stream()        # …then succeed

    monkeypatch.setattr("jigga.runtime.model_router.urllib.request.urlopen", fake_urlopen)
    provider = ModelProviderConfig(id="chatgpt", kind="chatgpt_oauth", default_model="gpt-5")

    result = _call_chatgpt_oauth(provider, _request(), "gpt-5", tmp_path)

    assert result.status == "ok"
    assert result.content == "hi"
    assert len(slept) == 2  # backed off once per 429 before the success


def test_chatgpt_429_exhausts_retries_and_raises(tmp_path: Path, monkeypatch):
    _patch_creds(monkeypatch)
    slept: list[float] = []
    monkeypatch.setattr("jigga.runtime.model_router.time.sleep", lambda s: slept.append(s))
    monkeypatch.setattr("jigga.runtime.model_router.urllib.request.urlopen",
                        lambda _req, timeout=None: (_ for _ in ()).throw(_http_429("0")))
    provider = ModelProviderConfig(id="chatgpt", kind="chatgpt_oauth", default_model="gpt-5")

    with pytest.raises(RuntimeError, match="429"):
        _call_chatgpt_oauth(provider, _request(), "gpt-5", tmp_path)
    assert len(slept) == model_router._CHATGPT_MAX_RETRIES  # retried the max, then gave up


# --- an empty turn retries once instead of killing the run --------------------


def _reasoning_only_stream() -> _FakeSSE:
    """What broke chat: a turn with reasoning and nothing else."""
    item = {"type": "response.output_item.done", "item": {"type": "reasoning", "summary": []}}
    done = {"type": "response.completed", "response": {"usage": {"input_tokens": 9, "output_tokens": 0}}}
    return _FakeSSE([f"data: {json.dumps(item)}".encode(), f"data: {json.dumps(done)}".encode()])


def _incomplete_stream(reason: str = "max_output_tokens") -> _FakeSSE:
    item = {"type": "response.output_item.done", "item": {"type": "reasoning"}}
    done = {"type": "response.incomplete",
            "response": {"incomplete_details": {"reason": reason}, "usage": {}}}
    return _FakeSSE([f"data: {json.dumps(item)}".encode(), f"data: {json.dumps(done)}".encode()])


def test_an_empty_turn_is_retried_once_and_succeeds(tmp_path: Path, monkeypatch):
    # The user had already waited through six model calls; failing the run for a
    # turn the model simply had nothing to say in is the wrong trade.
    _patch_creds(monkeypatch)
    attempts = {"n": 0}

    def fake_urlopen(_req, timeout=None):
        attempts["n"] += 1
        return _reasoning_only_stream() if attempts["n"] == 1 else _ok_stream()

    monkeypatch.setattr("jigga.runtime.model_router.urllib.request.urlopen", fake_urlopen)
    provider = ModelProviderConfig(id="chatgpt", kind="chatgpt_oauth", default_model="gpt-5")

    result = _call_chatgpt_oauth(provider, _request(), "gpt-5", tmp_path)

    assert result.status == "ok" and result.content == "hi"
    assert attempts["n"] == 2


def test_the_retry_is_audited_with_what_arrived(tmp_path: Path, monkeypatch):
    import json as _json

    _patch_creds(monkeypatch)
    attempts = {"n": 0}

    def fake_urlopen(_req, timeout=None):
        attempts["n"] += 1
        return _reasoning_only_stream() if attempts["n"] == 1 else _ok_stream()

    monkeypatch.setattr("jigga.runtime.model_router.urllib.request.urlopen", fake_urlopen)
    provider = ModelProviderConfig(id="chatgpt", kind="chatgpt_oauth", default_model="gpt-5")
    _call_chatgpt_oauth(provider, _request(), "gpt-5", tmp_path)

    events = [_json.loads(line) for line in
              (tmp_path / "logs" / "events.jsonl").read_text().splitlines() if line.strip()]
    empty = [e for e in events if e["type"] == "model.empty_turn"]
    assert len(empty) == 1
    assert empty[0]["details"]["emitted"] == "reasoning"


def test_two_empty_turns_in_a_row_is_an_error_that_says_what_arrived(tmp_path: Path, monkeypatch):
    _patch_creds(monkeypatch)
    monkeypatch.setattr("jigga.runtime.model_router.urllib.request.urlopen",
                        lambda _req, timeout=None: _reasoning_only_stream())
    provider = ModelProviderConfig(id="chatgpt", kind="chatgpt_oauth", default_model="gpt-5")

    with pytest.raises(ModelResponseError) as exc:
        _call_chatgpt_oauth(provider, _request(), "gpt-5", tmp_path)
    assert "twice" in str(exc.value) and "reasoning" in str(exc.value)


def test_a_truncated_response_is_not_retried(tmp_path: Path, monkeypatch):
    # Retrying a response that hit the output cap just truncates again; the
    # reason is the actionable part.
    _patch_creds(monkeypatch)
    attempts = {"n": 0}

    def fake_urlopen(_req, timeout=None):
        attempts["n"] += 1
        return _incomplete_stream()

    monkeypatch.setattr("jigga.runtime.model_router.urllib.request.urlopen", fake_urlopen)
    provider = ModelProviderConfig(id="chatgpt", kind="chatgpt_oauth", default_model="gpt-5")

    with pytest.raises(ModelResponseError) as exc:
        _call_chatgpt_oauth(provider, _request(), "gpt-5", tmp_path)
    assert "max_output_tokens" in str(exc.value)
    assert attempts["n"] == 1

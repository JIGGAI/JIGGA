from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest

from jigga.runtime import chatgpt_auth
from jigga.runtime.chatgpt_auth import ChatGptAuthError, load_credentials
from jigga.runtime.model_router import (
    ModelCallItem,
    ModelCallRequest,
    ModelToolCall,
    _build_responses_payload,
    parse_responses_stream,
)


def _jwt(claims: dict) -> str:
    """A fake unsigned JWT (header.payload.sig) carrying the given claims."""
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


def _request(items, tools=None) -> ModelCallRequest:
    return ModelCallRequest(agent_id="a", role="r", task={"id": "t", "title": "x"}, items=items, tools=tools)


# --- request builder -------------------------------------------------------


def test_build_payload_system_becomes_instructions_user_becomes_input() -> None:
    req = _request([
        ModelCallItem(id="s", role="system", content="You are terse."),
        ModelCallItem(id="u", role="user", content="Hello"),
    ])
    payload = _build_responses_payload(req, "gpt-5.5")
    assert payload["instructions"] == "You are terse."
    assert payload["store"] is False
    assert payload["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello"}]}
    ]


def test_build_payload_maps_tool_calls_and_results() -> None:
    req = _request([
        ModelCallItem(role="assistant", content="", tool_calls=[ModelToolCall(id="c1", name="get_weather", arguments={"city": "Paris"})]),
        ModelCallItem(role="tool", content='{"temp": 20}', tool_call_id="c1"),
    ])
    items = _build_responses_payload(req, "gpt-5.5")["input"]
    assert items[0] == {"type": "function_call", "call_id": "c1", "name": "get_weather", "arguments": '{"city": "Paris"}'}
    assert items[1] == {"type": "function_call_output", "call_id": "c1", "output": '{"temp": 20}'}


def test_build_payload_flattens_tools() -> None:
    tools = [{"type": "function", "function": {"name": "get_weather", "description": "w", "parameters": {"type": "object"}}}]
    payload = _build_responses_payload(_request([ModelCallItem(id="u", role="user", content="hi")], tools=tools), "gpt-5.5")
    assert payload["tools"] == [{"type": "function", "name": "get_weather", "description": "w", "parameters": {"type": "object"}}]


# --- SSE parser ------------------------------------------------------------


def _sse(*events: dict) -> list[str]:
    return [f"data: {json.dumps(e)}" for e in events] + ["data: [DONE]"]


def test_parse_stream_collects_text_and_usage() -> None:
    out = parse_responses_stream(_sse(
        {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "JIGGA online"}]}},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 22, "output_tokens": 8}}},
    ))
    assert out["content"] == "JIGGA online"
    assert out["input_tokens"] == 22 and out["output_tokens"] == 8
    assert out["tool_calls"] == []


def test_parse_stream_collects_function_call() -> None:
    out = parse_responses_stream(_sse(
        {"type": "response.output_item.done", "item": {"type": "function_call", "name": "get_weather",
         "call_id": "call_1", "arguments": '{"city":"Paris"}'}},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 66, "output_tokens": 18}}},
    ))
    assert len(out["tool_calls"]) == 1
    call = out["tool_calls"][0]
    assert (call.id, call.name, call.arguments) == ("call_1", "get_weather", {"city": "Paris"})


def test_parse_stream_tolerates_bytes_and_junk() -> None:
    lines = [b'data: {"type":"response.in_progress"}', b": comment", b"data: not-json",
             *[s.encode() for s in _sse({"type": "response.output_item.done",
               "item": {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}})]]
    assert parse_responses_stream(lines)["content"] == "ok"


# --- credential loader -----------------------------------------------------


def test_load_credentials_reads_store_and_account_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(chatgpt_auth, "codex_store", lambda: tmp_path / "nope.json")
    access = _jwt({"exp": int(time.time()) + 3600, "https://api.openai.com/auth": {"chatgpt_account_id": "acct_9"}})
    chatgpt_auth.save_credentials(tmp_path, {"access_token": access, "refresh_token": "r"})
    creds = load_credentials(home=tmp_path)
    assert creds.access_token == access
    assert creds.account_id == "acct_9"


def test_load_credentials_refreshes_when_expired(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(chatgpt_auth, "codex_store", lambda: tmp_path / "nope.json")
    expired = _jwt({"exp": int(time.time()) - 10})
    fresh = _jwt({"exp": int(time.time()) + 3600, "https://api.openai.com/auth": {"chatgpt_account_id": "acct_new"}})
    chatgpt_auth.save_credentials(tmp_path, {"access_token": expired, "refresh_token": "r0"})
    monkeypatch.setattr(chatgpt_auth, "_refresh", lambda rt: {"access_token": fresh, "refresh_token": "r1"})
    creds = load_credentials(home=tmp_path)
    assert creds.access_token == fresh
    assert creds.account_id == "acct_new"
    # rotated tokens persisted back to the JIGGA store
    saved = json.loads(chatgpt_auth.jigga_store(tmp_path).read_text())["tokens"]
    assert saved["access_token"] == fresh and saved["refresh_token"] == "r1"


def test_load_credentials_missing_store_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(chatgpt_auth, "codex_store", lambda: tmp_path / "nope.json")
    with pytest.raises(ChatGptAuthError):
        load_credentials(home=tmp_path)

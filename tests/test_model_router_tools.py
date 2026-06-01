from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch


from jigga.commands.init import init_runtime
from jigga.runtime.model_router import (
    ModelCallItem,
    ModelCallRequest,
    ModelToolCall,
    _parse_tool_calls,
    call_model,
)


def _req(home_dry_run: bool = True, **overrides) -> ModelCallRequest:
    base = dict(
        agent_id="a",
        role="r",
        task={"id": "t1", "title": "do it"},
        items=[ModelCallItem(id="sys", role="system", content="be helpful")],
        dry_run=home_dry_run,
    )
    base.update(overrides)
    return ModelCallRequest(**base)


# --- ModelCallItem provider message shapes ---------------------------------


def test_tool_result_message_includes_tool_call_id() -> None:
    item = ModelCallItem(role="tool", content="result text", tool_call_id="call_1")
    msg = item.to_provider_message()
    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "call_1"
    assert msg["content"] == "result text"


def test_assistant_tool_call_message_serializes_calls() -> None:
    item = ModelCallItem(
        role="assistant",
        content="",
        tool_calls=[ModelToolCall(id="c1", name="telegram.send_message", arguments={"chat_id": 5, "text": "hi"})],
    )
    msg = item.to_provider_message()
    assert msg["tool_calls"][0]["id"] == "c1"
    assert msg["tool_calls"][0]["function"]["name"] == "telegram.send_message"
    # arguments are JSON-encoded per OpenAI's schema
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"chat_id": 5, "text": "hi"}


def test_plain_message_has_no_tool_fields() -> None:
    msg = ModelCallItem(role="user", content="hello").to_provider_message()
    assert "tool_call_id" not in msg
    assert "tool_calls" not in msg


# --- dry_run scripting hook ------------------------------------------------


def test_dry_run_returns_text_by_default(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    result = call_model(tmp_path, tmp_path / "logs", _req())
    assert result.tool_calls == []
    assert "Dry-run model response" in result.content


def test_dry_run_replays_scripted_tool_calls(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    scripted = [ModelToolCall(id="c1", name="filesystem.read_file", arguments={"path": "~/x"})]
    result = call_model(tmp_path, tmp_path / "logs", _req(dry_run_tool_calls=scripted))
    assert result.content == ""
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "filesystem.read_file"
    assert result.tool_calls[0].arguments == {"path": "~/x"}


# --- _parse_tool_calls -----------------------------------------------------


def test_parse_tool_calls_happy_path() -> None:
    raw = [
        {"id": "c1", "type": "function", "function": {"name": "a.b", "arguments": '{"x": 1}'}},
    ]
    calls = _parse_tool_calls(raw)
    assert calls[0].id == "c1"
    assert calls[0].name == "a.b"
    assert calls[0].arguments == {"x": 1}


def test_parse_tool_calls_tolerates_bad_json_arguments() -> None:
    raw = [{"id": "c1", "function": {"name": "a.b", "arguments": "not-json"}}]
    calls = _parse_tool_calls(raw)
    assert calls[0].arguments == {"_raw": "not-json"}


def test_parse_tool_calls_skips_nameless_and_handles_empty() -> None:
    assert _parse_tool_calls(None) == []
    assert _parse_tool_calls([{"id": "c1", "function": {}}]) == []


def test_parse_tool_calls_accepts_dict_arguments() -> None:
    # Some providers may already give a dict rather than a JSON string.
    raw = [{"id": "c1", "function": {"name": "a.b", "arguments": {"x": 2}}}]
    assert _parse_tool_calls(raw)[0].arguments == {"x": 2}


# --- openai_compatible transport (mocked) ----------------------------------


def _fake_urlopen(body: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(body).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = response
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


def _openai_config(tmp_path: Path) -> None:
    from jigga.core.io import read_yaml, write_yaml
    config = read_yaml(tmp_path / "config.yaml")
    config["models"] = {
        "defaults": {"provider": "openai"},
        "providers": {
            "openai": {
                "kind": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
                "default_model": "gpt-4o-mini",
            }
        },
        "profiles": {"default": {"primary": "openai", "fallback": []}},
    }
    write_yaml(tmp_path / "config.yaml", config)


def test_openai_passes_tools_and_parses_tool_calls(tmp_path: Path, monkeypatch) -> None:
    init_runtime(tmp_path)
    _openai_config(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    captured: dict = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {"id": "c1", "type": "function",
                                 "function": {"name": "telegram.send_message", "arguments": '{"chat_id": 7, "text": "hi"}'}}
                            ],
                        }
                    }
                ]
            }
        ).encode("utf-8")
        cm = MagicMock()
        cm.__enter__.return_value = response
        cm.__exit__.return_value = False
        return cm

    tools = [{"type": "function", "function": {"name": "telegram.send_message", "parameters": {}}}]
    request = ModelCallRequest(
        agent_id="a", role="r", task={"id": "t", "title": "reply"},
        items=[ModelCallItem(id="sys", role="system", content="x")],
        dry_run=False, tools=tools,
    )
    with patch("jigga.runtime.model_router.urllib.request.urlopen", fake_urlopen):
        result = call_model(tmp_path, tmp_path / "logs", request)

    # tools were forwarded to the provider
    assert captured["body"]["tools"] == tools
    # tool_calls parsed back
    assert result.tool_calls[0].name == "telegram.send_message"
    assert result.tool_calls[0].arguments == {"chat_id": 7, "text": "hi"}


def test_openai_no_content_no_tools_raises(tmp_path: Path, monkeypatch) -> None:
    init_runtime(tmp_path)
    _openai_config(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    body = {"choices": [{"message": {"content": None}}]}
    request = ModelCallRequest(
        agent_id="a", role="r", task={"id": "t", "title": "x"},
        items=[ModelCallItem(id="sys", role="system", content="x")],
        dry_run=False,
    )
    with patch("jigga.runtime.model_router.urllib.request.urlopen", _fake_urlopen(body)):
        result = call_model(tmp_path, tmp_path / "logs", request)
    # call_model swallows provider errors into an error result (fallback boundary)
    assert result.status == "error"
    assert "neither content nor tool_calls" in result.error


def test_result_to_dict_serializes_tool_calls(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    scripted = [ModelToolCall(id="c1", name="a.b", arguments={"x": 1})]
    result = call_model(tmp_path, tmp_path / "logs", _req(dry_run_tool_calls=scripted))
    payload = result.to_dict()
    assert payload["tool_calls"][0]["name"] == "a.b"
    assert payload["tool_calls"][0]["arguments"] == {"x": 1}


def test_provider_fallback_used_when_primary_fails(tmp_path: Path, monkeypatch) -> None:
    """If the primary provider raises, the router must fall through to the
    configured fallback and flag fallback_used. This path had no test."""
    from jigga.core.io import read_yaml, write_yaml
    init_runtime(tmp_path)
    config = read_yaml(tmp_path / "config.yaml")
    config["models"] = {
        "defaults": {"provider": "primary"},
        "providers": {
            "primary": {"kind": "openai_compatible", "base_url": "https://primary.example/v1",
                        "api_key_env": "OPENAI_API_KEY", "default_model": "m1"},
            "secondary": {"kind": "openai_compatible", "base_url": "https://secondary.example/v1",
                          "api_key_env": "OPENAI_API_KEY", "default_model": "m2"},
        },
        "profiles": {"default": {"primary": "primary", "fallback": ["secondary"]}},
    }
    write_yaml(tmp_path / "config.yaml", config)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("primary down")
        response = MagicMock()
        response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": "from secondary"}}]}).encode("utf-8")
        cm = MagicMock()
        cm.__enter__.return_value = response
        cm.__exit__.return_value = False
        return cm

    request = ModelCallRequest(agent_id="a", role="r", task={"id": "t", "title": "x"},
                               items=[ModelCallItem(id="sys", role="system", content="x")], dry_run=False)
    with patch("jigga.runtime.model_router.urllib.request.urlopen", fake_urlopen):
        result = call_model(tmp_path, tmp_path / "logs", request)

    assert calls["n"] == 2                      # primary tried, then secondary
    assert result.status == "ok"
    assert result.content == "from secondary"
    assert result.fallback_used is True


def test_parse_responses_stream_tolerates_malformed_items() -> None:
    """Hostile/malformed provider output must not crash the parser (non-list
    content, non-dict item, non-dict usage)."""
    from jigga.runtime.model_router import parse_responses_stream
    lines = [
        'data: {"type":"response.output_item.done","item":{"type":"message","content":"not-a-list"}}',
        'data: {"type":"response.output_item.done","item":"not-a-dict"}',
        'data: {"type":"response.completed","response":{"usage":"not-a-dict"}}',
    ]
    out = parse_responses_stream(lines)
    assert out == {"content": "", "tool_calls": [], "input_tokens": 0, "output_tokens": 0}


def test_parse_tool_calls_tolerates_malformed_entries() -> None:
    from jigga.runtime.model_router import _parse_tool_calls
    calls = _parse_tool_calls(["junk", {"function": "not-a-dict"}, 5,
                               {"function": {"name": "a.b", "arguments": "{}"}}])
    assert [c.name for c in calls] == ["a.b"]   # only the valid one survives, no crash

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.core.models import WorkflowStep
from jigga.runtime.telegram import (
    SUPPORTED_ACTIONS,
    allowed_chat_ids,
    bot_token_path,
    load_bot_token,
    load_offset,
    poll_messages,
    send_message,
    store_bot_token,
    store_offset,
    telegram_handler,
)


@dataclass
class _StubRuntime:
    home: Path
    agent: object = None


def _step(action: str, input_dict: dict | None = None) -> WorkflowStep:
    return WorkflowStep(id="t", action=action, input=input_dict or {})


def _fake_urlopen(body: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(body).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = response
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


def _update(update_id: int, chat_id: int, text: str, username: str = "alice") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": {"id": chat_id, "username": username},
            "chat": {"id": chat_id, "type": "private"},
            "date": 1700000000,
            "text": text,
        },
    }


def _set_allowlist(paths, ids: list[str]) -> None:
    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": True, "allowed_chat_ids": ids, "default_agent": "daily_briefing_agent"}}
    write_yaml(paths.config, config)


# --- token + offset storage ------------------------------------------------


def test_bot_token_round_trip(tmp_path: Path) -> None:
    assert load_bot_token(tmp_path) is None
    store_bot_token(tmp_path, "123:abc")
    assert load_bot_token(tmp_path) == "123:abc"


def test_offset_round_trip(tmp_path: Path) -> None:
    assert load_offset(tmp_path) == 0
    store_offset(tmp_path, 42)
    assert load_offset(tmp_path) == 42


# --- allowlist config ------------------------------------------------------


def test_allowed_chat_ids_reads_config(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_allowlist(paths, ["111", "222"])
    assert allowed_chat_ids(paths.home) == {"111", "222"}


# --- poll: not connected ---------------------------------------------------


def test_poll_not_connected_without_token(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    result = poll_messages(paths.home)
    assert result["status"] == "telegram.not_connected"


# --- poll: allowlist filtering + offset advance ----------------------------


def test_poll_filters_to_allowlist_and_advances_offset(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    store_bot_token(paths.secrets, "123:abc")
    _set_allowlist(paths, ["111"])
    body = {
        "ok": True,
        "result": [
            _update(5, 111, "hello from allowed"),
            _update(6, 999, "spam from stranger"),
        ],
    }
    with patch("jigga.runtime.telegram.urllib.request.urlopen", _fake_urlopen(body)):
        result = poll_messages(paths.home)
    assert result["status"] == "ok"
    assert [m["text"] for m in result["messages"]] == ["hello from allowed"]
    assert result["dropped"] == 1
    # offset advanced past the highest update_id (6) → 7
    assert load_offset(paths.home) == 7


def test_poll_default_denies_with_empty_allowlist(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    store_bot_token(paths.secrets, "123:abc")
    # no allowlist configured
    body = {"ok": True, "result": [_update(1, 111, "hi")]}
    with patch("jigga.runtime.telegram.urllib.request.urlopen", _fake_urlopen(body)):
        result = poll_messages(paths.home)
    assert result["messages"] == []
    assert result["dropped"] == 1
    assert "default-deny" in result["note"]
    # but offset still advanced (we consumed the updates)
    assert load_offset(paths.home) == 2


def test_discover_bypasses_allowlist_and_preserves_offset(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    store_bot_token(paths.secrets, "123:abc")
    body = {"ok": True, "result": [_update(8, 999, "find me")]}
    with patch("jigga.runtime.telegram.urllib.request.urlopen", _fake_urlopen(body)):
        result = poll_messages(paths.home, discover=True)
    assert result["discover"] is True
    assert [m["chat_id"] for m in result["messages"]] == [999]
    # discover must NOT advance the offset
    assert load_offset(paths.home) == 0


def test_poll_includes_edited_messages(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    store_bot_token(paths.secrets, "123:abc")
    _set_allowlist(paths, ["111"])
    body = {
        "ok": True,
        "result": [
            {
                "update_id": 3,
                "edited_message": {
                    "message_id": 30,
                    "from": {"id": 111, "username": "alice"},
                    "chat": {"id": 111, "type": "private"},
                    "date": 1700000000,
                    "text": "edited text",
                },
            }
        ],
    }
    with patch("jigga.runtime.telegram.urllib.request.urlopen", _fake_urlopen(body)):
        result = poll_messages(paths.home)
    assert result["messages"][0]["text"] == "edited text"


# --- send ------------------------------------------------------------------


def test_send_message_posts_and_returns_id(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    store_bot_token(paths.secrets, "123:abc")
    body = {"ok": True, "result": {"message_id": 555}}
    with patch("jigga.runtime.telegram.urllib.request.urlopen", _fake_urlopen(body)):
        result = send_message(paths.home, 111, "hi there")
    assert result["sent"] is True
    assert result["message_id"] == 555
    assert result["chat_id"] == 111


def test_send_not_connected_without_token(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    result = send_message(paths.home, 111, "hi")
    assert result["status"] == "telegram.not_connected"


def test_api_raises_on_not_ok(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    store_bot_token(paths.secrets, "123:abc")
    body = {"ok": False, "description": "Unauthorized"}
    with patch("jigga.runtime.telegram.urllib.request.urlopen", _fake_urlopen(body)):
        with pytest.raises(RuntimeError, match="Unauthorized"):
            send_message(paths.home, 111, "hi")


# --- handler dispatch ------------------------------------------------------


def test_handler_poll(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    store_bot_token(paths.secrets, "123:abc")
    _set_allowlist(paths, ["111"])
    runtime = _StubRuntime(home=paths.home)
    body = {"ok": True, "result": [_update(1, 111, "hi")]}
    with patch("jigga.runtime.telegram.urllib.request.urlopen", _fake_urlopen(body)):
        result = telegram_handler(_step("telegram.poll_messages"), None, {}, {}, runtime)
    assert result["source"] == "capability.telegram"
    assert result["messages"][0]["text"] == "hi"


def test_handler_send_requires_fields(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    runtime = _StubRuntime(home=paths.home)
    with pytest.raises(ValueError, match="chat_id.*text"):
        telegram_handler(_step("telegram.send_message", {"chat_id": 1}), None, {"chat_id": 1}, {}, runtime)


def test_handler_unknown_action(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    runtime = _StubRuntime(home=paths.home)
    with pytest.raises(ValueError, match="Unknown telegram action"):
        telegram_handler(_step("telegram.delete_account"), None, {}, {}, runtime)


# --- registration ----------------------------------------------------------


def test_telegram_in_optional_registry() -> None:
    from jigga.optional_capabilities import REGISTRY
    assert "telegram" in REGISTRY


def test_telegram_handler_registered() -> None:
    from jigga.runtime.dispatcher import HANDLERS
    assert HANDLERS.get("runtime.telegram") is not None


def test_supported_actions_match_manifest() -> None:
    import yaml
    manifest = Path(__file__).resolve().parents[1] / "jigga" / "optional_capabilities" / "telegram" / "manifest.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert set(data["actions"]) == set(SUPPORTED_ACTIONS)


# --- CLI -------------------------------------------------------------------


def test_cli_status_smoke(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "telegram", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["token_present"] is False
    assert payload["allowed_chat_ids"] == []


def test_cli_logout_idempotent(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "telegram", "logout"]) == 0
    assert "No stored Telegram bot token" in capsys.readouterr().out
    store_bot_token(paths.secrets, "123:abc")
    assert main(["--home", str(tmp_path), "telegram", "logout"]) == 0
    assert "Removed stored Telegram bot token" in capsys.readouterr().out


# --- setup wizard ----------------------------------------------------------


def test_setup_wizard_stores_token_and_config(tmp_path: Path) -> None:
    from jigga.optional_capabilities.telegram import setup
    paths = init_runtime(tmp_path)
    inputs = iter([
        "123:abc",   # token
        "n",          # discover? no
        "111,222",    # allowed chat ids
        "daily_briefing_agent",  # default agent
    ])
    exit_code = setup(paths, input_fn=lambda _: next(inputs), print_fn=lambda *a, **k: None)
    assert exit_code == 0
    assert load_bot_token(paths.secrets) == "123:abc"
    assert allowed_chat_ids(paths.home) == {"111", "222"}
    config = read_yaml(paths.config)
    assert config["channels"]["telegram"]["enabled"] is True
    assert config["channels"]["telegram"]["default_agent"] == "daily_briefing_agent"


def test_setup_wizard_discovery_prefills_chat_ids(tmp_path: Path) -> None:
    from jigga.optional_capabilities.telegram import setup
    paths = init_runtime(tmp_path)

    def fake_poller(home, discover=False):
        return {"messages": [{"chat_id": 777, "sender": "bob"}]}

    inputs = iter([
        "123:abc",   # token
        "y",          # discover? yes
        "",           # accept discovered default (777)
        "",           # default agent → default
    ])
    exit_code = setup(
        paths,
        input_fn=lambda _: next(inputs),
        print_fn=lambda *a, **k: None,
        poller=fake_poller,
    )
    assert exit_code == 0
    assert allowed_chat_ids(paths.home) == {"777"}

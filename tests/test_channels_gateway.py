from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from jigga.runtime.channels import (
    ADAPTERS,
    ChannelAdapter,
    JiggaEvent,
    TelegramAdapter,
    get_adapter,
    identity_allowed,
)


def _event(chat_id=111, actor_id=111, text="hi") -> JiggaEvent:
    return JiggaEvent(
        source="telegram",
        actor={"type": "user", "id": actor_id, "name": "alice"},
        conversation={"id": chat_id},
        message={"text": text, "attachments": []},
    )


def test_jigga_event_shape_and_id() -> None:
    ev = _event(text="hello")
    assert ev.text == "hello"
    assert ev.conversation_id == 111
    assert ev.actor_id == 111
    assert ev.actor_name == "alice"
    assert ev.id.startswith("evt_ch_")


def test_telegram_adapter_normalizes_message() -> None:
    msg = {"chat_id": 5, "sender": "bob", "sender_id": 5, "text": "yo", "message_id": 9}
    ev = TelegramAdapter.to_event(msg)
    assert ev.source == "telegram"
    assert ev.conversation_id == 5
    assert ev.actor == {"type": "user", "id": 5, "name": "bob"}
    assert ev.text == "yo"
    assert ev.raw["message_id"] == 9


def test_telegram_adapter_poll_maps_messages_to_events() -> None:
    poll_result = {"status": "ok", "messages": [
        {"chat_id": 1, "sender": "a", "sender_id": 1, "text": "one", "message_id": 1},
        {"chat_id": 2, "sender": "b", "sender_id": 2, "text": "two", "message_id": 2},
    ]}
    with patch("jigga.runtime.telegram.poll_messages", return_value=poll_result):
        out = TelegramAdapter().poll(Path("/nope"), long_poll_seconds=0)
    assert out["status"] == "ok"
    assert [e.text for e in out["events"]] == ["one", "two"]
    assert all(isinstance(e, JiggaEvent) for e in out["events"])


def test_telegram_adapter_propagates_error_status() -> None:
    with patch("jigga.runtime.telegram.poll_messages", return_value={"status": "telegram.not_connected", "messages": []}):
        out = TelegramAdapter().poll(Path("/nope"))
    assert out["status"] == "telegram.not_connected"
    assert out["events"] == []


def test_identity_allowed_rules() -> None:
    # allowlist set + match (by conversation or actor)
    assert identity_allowed(_event(chat_id=111), {"allowed_chat_ids": ["111"]})
    assert identity_allowed(_event(chat_id=999, actor_id=111), {"allowed_chat_ids": ["111"]})
    # allowlist set + no match → denied
    assert not identity_allowed(_event(chat_id=222, actor_id=222), {"allowed_chat_ids": ["111"]})
    # no allowlist → adapter-level policy governs (gateway permits)
    assert identity_allowed(_event(), {})


def test_registry_and_contract() -> None:
    assert get_adapter("telegram") is ADAPTERS["telegram"]
    assert get_adapter("nope") is None
    assert isinstance(TelegramAdapter(), ChannelAdapter)  # runtime_checkable Protocol

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.channel_listener import ingest_once
from jigga.runtime.channels import JiggaEvent, TelegramAdapter, activation_allows, from_public_conversation
from jigga.runtime.tasks import list_tasks


def _event(*, chat_type="private", mentions_bot=False, chat_id=111) -> JiggaEvent:
    return JiggaEvent(
        source="telegram",
        actor={"type": "user", "id": chat_id, "name": "alice"},
        conversation={"id": chat_id, "type": chat_type},
        message={"text": "hi", "attachments": [], "mentions_bot": mentions_bot},
    )


# --- activation_allows matrix ----------------------------------------------


def test_activation_always_and_disabled() -> None:
    assert activation_allows(_event(), {"activation": "always"})
    assert activation_allows(_event(chat_type="group"), {})  # default = always
    assert not activation_allows(_event(), {"activation": "disabled"})


def test_activation_direct_message_only() -> None:
    cfg = {"activation": "direct_message_only"}
    assert activation_allows(_event(chat_type="private"), cfg)
    assert not activation_allows(_event(chat_type="group"), cfg)


def test_activation_mention() -> None:
    cfg = {"activation": "mention"}
    assert activation_allows(_event(chat_type="private", mentions_bot=False), cfg)        # DMs always
    assert activation_allows(_event(chat_type="group", mentions_bot=True), cfg)           # group + mention
    assert not activation_allows(_event(chat_type="group", mentions_bot=False), cfg)      # group, no mention


def test_activation_unknown_mode_is_permissive() -> None:
    assert activation_allows(_event(chat_type="group"), {"activation": "weird"})


def test_from_public_conversation() -> None:
    assert from_public_conversation(_event(chat_type="group"))
    assert not from_public_conversation(_event(chat_type="private"))


# --- adapter surfaces the new fields ---------------------------------------


def test_adapter_surfaces_chat_type_and_mention() -> None:
    ev = TelegramAdapter.to_event(
        {"chat_id": 5, "chat_type": "group", "sender": "bob", "sender_id": 5,
         "text": "@bot hi", "mentions_bot": True, "message_id": 1}
    )
    assert ev.conversation_type == "group"
    assert ev.is_direct is False
    assert ev.mentions_bot is True


# --- end-to-end through the listener ---------------------------------------


def _enable(paths, **extra) -> None:
    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": True, "allowed_chat_ids": ["111"],
                                       "default_agent": "daily_briefing_agent", **extra}}
    write_yaml(paths.config, config)


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _poll(chat_type, mentions_bot):
    return {"status": "ok", "messages": [{"channel": "telegram", "chat_id": 111, "chat_type": chat_type,
            "sender": "alice", "sender_id": 111, "text": "do it", "mentions_bot": mentions_bot, "message_id": 9}]}


def test_listener_ignores_unmentioned_group_message(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable(paths, activation="mention")
    with patch("jigga.runtime.telegram.poll_messages", return_value=_poll("group", mentions_bot=False)), \
         patch("jigga.runtime.channel_listener.run_agent"):
        summary = ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0)
    assert summary["created"] == []
    assert list_tasks(paths.tasks) == []
    assert "channel.message.ignored" in [e["type"] for e in _events(paths)]


def test_listener_acts_on_mentioned_group_message_and_tags_restricted(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable(paths, activation="mention")
    with patch("jigga.runtime.telegram.poll_messages", return_value=_poll("group", mentions_bot=True)), \
         patch("jigga.runtime.channel_listener.run_agent"):
        summary = ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0)
    assert len(summary["created"]) == 1
    task = list_tasks(paths.tasks)[0]
    assert task.metadata["conversation_type"] == "group"
    assert task.metadata["restricted_memory"] is True

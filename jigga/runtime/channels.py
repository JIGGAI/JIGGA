"""Channel gateway — the normalized boundary between external surfaces and the
runtime, per `docs/tools/CHANNEL_GATEWAY_MESSAGE_ADAPTERS.md`.

Every channel (Telegram today; Slack / webhook / iMessage later) implements one
`ChannelAdapter` contract and produces one normalized `JiggaEvent` shape. The
listener/supervisor then route every channel through the same path:

    adapter.poll → JiggaEvent[] → identity/policy check → task → agent

Adding a channel = implement `ChannelAdapter`, register it in `ADAPTERS`. No
listener changes. This is the layer the ad-hoc Telegram path skipped (B1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from jigga.runtime import telegram
from jigga.runtime.audit import new_id


@dataclass
class JiggaEvent:
    """A normalized inbound event — the single shape every channel produces.

    Mirrors the channel-gateway spec: source / actor / conversation / message /
    target. `raw` carries the adapter's original normalized dict for metadata
    (message_id, etc.); `id` correlates the event through the audit log.
    """

    source: str
    actor: dict[str, Any]                      # {type, id, name}
    conversation: dict[str, Any]               # {id}
    message: dict[str, Any]                     # {text, attachments}
    target: dict[str, Any] = field(default_factory=dict)   # {agent}
    raw: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = new_id("evt_ch")

    @property
    def text(self) -> str:
        return self.message.get("text") or ""

    @property
    def conversation_id(self) -> Any:
        return self.conversation.get("id")

    @property
    def actor_id(self) -> Any:
        return self.actor.get("id")

    @property
    def actor_name(self) -> str:
        return str(self.actor.get("name") or "unknown")

    @property
    def conversation_type(self) -> str | None:
        return self.conversation.get("type")

    @property
    def is_direct(self) -> bool:
        """A 1:1 / DM conversation (vs a group/channel)."""
        return self.conversation.get("type") == "private"

    @property
    def mentions_bot(self) -> bool:
        return bool(self.message.get("mentions_bot"))


@runtime_checkable
class ChannelAdapter(Protocol):
    """The contract every channel implements. `poll` returns
    `{"status": "ok"|<error>, "events": [JiggaEvent, ...]}`; `send` delivers an
    outbound reply to a conversation."""

    name: str

    def poll(self, home: Path, *, long_poll_seconds: int = 0) -> dict[str, Any]: ...

    def send(self, home: Path, *, conversation_id: Any, text: str) -> dict[str, Any]: ...


class TelegramAdapter:
    """Telegram on the `ChannelAdapter` contract. Delegates to `runtime.telegram`
    (kept as the wire-level primitives + capability handler) and maps its
    normalized messages into `JiggaEvent`s."""

    name = "telegram"

    def poll(self, home: Path, *, long_poll_seconds: int = 0) -> dict[str, Any]:
        result = telegram.poll_messages(home, long_poll_seconds=long_poll_seconds)
        status = result.get("status", "ok")
        events = [self.to_event(message) for message in result.get("messages", [])]
        return {"status": status, "events": events, "raw": result}

    def send(self, home: Path, *, conversation_id: Any, text: str) -> dict[str, Any]:
        return telegram.send_message(home, conversation_id, text)

    @staticmethod
    def to_event(message: dict[str, Any]) -> JiggaEvent:
        return JiggaEvent(
            source="telegram",
            actor={"type": "user", "id": message.get("sender_id"), "name": message.get("sender")},
            conversation={"id": message.get("chat_id"), "type": message.get("chat_type")},
            message={"text": message.get("text") or "", "attachments": [],
                     "mentions_bot": bool(message.get("mentions_bot"))},
            raw=message,
        )


# Channel name -> adapter. Slack / iMessage / webhook register here once built.
ADAPTERS: dict[str, ChannelAdapter] = {"telegram": TelegramAdapter()}


def get_adapter(name: str) -> ChannelAdapter | None:
    return ADAPTERS.get(name)


def identity_allowed(event: JiggaEvent, cfg: dict[str, Any]) -> bool:
    """Policy/identity gate — the canonical place a channel authorizes a sender.

    Today: a chat/actor allowlist. When `allowed_chat_ids` is set, only those
    conversations/actors pass (default-deny). When unset, the adapter's own
    policy governs (the Telegram capability already default-denies with no
    allowlist; a future adapter without self-filtering relies on this gate).
    """
    allow = {str(item) for item in (cfg.get("allowed_chat_ids") or [])}
    if not allow:
        return True
    return str(event.conversation_id) in allow or str(event.actor_id) in allow


def activation_allows(event: JiggaEvent, cfg: dict[str, Any]) -> bool:
    """Should this event wake the agent, given the channel's activation mode?

    - `always` (default): every message acts.
    - `direct_message_only`: only 1:1 / DM conversations.
    - `mention`: DMs always; in groups/channels only when the bot is addressed.
    - `disabled`: never (polled but inert).

    An unknown mode is permissive (we don't silently drop on a typo).
    """
    mode = str(cfg.get("activation") or "always").lower()
    if mode == "disabled":
        return False
    if mode == "always":
        return True
    if mode == "direct_message_only":
        return event.is_direct
    if mode == "mention":
        return event.is_direct or event.mentions_bot
    return True


def from_public_conversation(event: JiggaEvent) -> bool:
    """True for group/channel (non-DM) conversations — these should run with
    restricted memory (prompt-injection safety). The conversation type is
    recorded on the task; enforcing the restricted scope in the agent loop is a
    follow-up (needs per-task memory scoping)."""
    return event.conversation_type not in (None, "private")

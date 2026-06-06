"""Webchat channel — the browser as a JIGGA channel (M2, jiggaview's Chat page).

Same contract as Telegram, file-backed instead of HTTP: inbound messages are
appended to `channels/webchat/inbox.jsonl` (by `jigga webchat send` — the
jiggaview chat route shells it), the adapter's poll consumes them past a
stored offset, they ride the NORMAL channel pipeline (identity → task → agent
runs), and the agent replies with the `webchat.send_message` TOOL, which
appends `outbox.jsonl` — what the browser renders. Everything is a file:
auditable, greppable, replayable.

Latency: `jigga webchat send --wait` ingests inline and returns the reply in
the same invocation (synchronous chat UX); the supervisor's normal channel
poll is the backstop for anything sent without --wait (offset consumption
prevents double-processing). No secrets, no allowlist — webchat is reachable
only by whoever can reach the jiggaview host (RJ: tailnet is the moat).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, read_json, write_json
from jigga.core.models import now_iso
from jigga.runtime.audit import new_id

SUPPORTED_ACTIONS = ("webchat.send_message", "webchat.poll_messages")
DEFAULT_CONVERSATION = "web"


def _dir(home: Path) -> Path:
    return Path(home) / "channels" / "webchat"


def _inbox(home: Path) -> Path:
    return _dir(home) / "inbox.jsonl"


def _outbox(home: Path) -> Path:
    return _dir(home) / "outbox.jsonl"


def _offset_path(home: Path) -> Path:
    return Path(home) / "state" / "webchat_offset.json"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_offset(home: Path) -> int:
    path = _offset_path(home)
    try:
        return int(read_json(path).get("offset", 0)) if path.exists() else 0
    except Exception:  # noqa: BLE001 — corrupt offset → reprocess (idempotent-ish, never lose)
        return 0


def store_offset(home: Path, offset: int) -> None:
    ensure_dir(_offset_path(home).parent)
    write_json(_offset_path(home), {"offset": int(offset)})


def append_inbound(home: Path, text: str, *, conversation_id: str = DEFAULT_CONVERSATION,
                   sender: str = "you") -> dict[str, Any]:
    """The browser's send: append an inbound message (seq = line position)."""
    entry = {
        "id": new_id("wcm"),
        "conversation_id": str(conversation_id),
        "sender": str(sender),
        "text": str(text),
        "ts": now_iso(),
    }
    _append_jsonl(_inbox(home), entry)
    return entry


def poll_messages(home: Path, *, long_poll_seconds: int = 0, limit: int = 50) -> dict[str, Any]:
    """Adapter contract: new inbound messages past the offset, normalized like
    telegram's (`chat_id`/`sender`/`text`/`message_id`), offset advanced.
    `long_poll_seconds` is accepted but ignored — webchat is a local file, the
    --wait send path gives instant UX, and blocking the supervisor's
    sequential channel loop would starve the other channels."""
    home = Path(home)
    entries = _read_jsonl(_inbox(home))
    offset = load_offset(home)
    fresh = entries[offset:offset + max(1, limit)]
    if fresh:
        store_offset(home, offset + len(fresh))
    messages = [
        {
            "chat_id": entry.get("conversation_id") or DEFAULT_CONVERSATION,
            "chat_type": "private",
            "sender": entry.get("sender") or "you",
            "sender_id": entry.get("sender") or "you",
            "text": entry.get("text") or "",
            "message_id": entry.get("id"),
            "mentions_bot": False,
        }
        for entry in fresh
    ]
    return {"status": "ok", "messages": messages}


def send_message(home: Path, conversation_id: Any, text: str) -> dict[str, Any]:
    """Outbound: append to the outbox the browser renders."""
    entry = {
        "id": new_id("wcr"),
        "conversation_id": str(conversation_id or DEFAULT_CONVERSATION),
        "sender": "agent",
        "text": str(text),
        "ts": now_iso(),
    }
    _append_jsonl(_outbox(home), entry)
    return {"status": "ok", "message_id": entry["id"]}


def history(home: Path, *, conversation_id: str = DEFAULT_CONVERSATION,
            limit: int = 200) -> list[dict[str, Any]]:
    """The merged conversation, chronological — what the chat page renders."""
    merged = [e for e in _read_jsonl(_inbox(home)) + _read_jsonl(_outbox(home))
              if (e.get("conversation_id") or DEFAULT_CONVERSATION) == conversation_id]
    merged.sort(key=lambda e: str(e.get("ts") or ""))
    return merged[-limit:]


def webchat_handler(step, _capability, resolved_input, _memory_context, runtime) -> Any:
    """Capability handler: `webchat.send_message` is how an agent replies in
    the browser (poll is runtime-only — the ingest pipeline owns it)."""
    params = resolved_input if isinstance(resolved_input, dict) else {}
    if step.action == "webchat.send_message":
        text = params.get("text")
        if text is None:
            raise ValueError("webchat.send_message requires 'text' in input")
        result = send_message(runtime.home, params.get("chat_id") or params.get("conversation_id"),
                              str(text))
        result.setdefault("source", "capability.webchat")
        result.setdefault("action", step.action)
        return result
    raise ValueError(f"Unknown webchat action: {step.action!r}. "
                     f"Supported: {', '.join(SUPPORTED_ACTIONS)}.")


class WebchatAdapter:
    """Webchat on the ChannelAdapter contract (registered in channels.ADAPTERS)."""

    name = "webchat"
    long_polls = False  # local file read — returns instantly; never paces the supervisor loop

    def poll(self, home: Path, *, long_poll_seconds: int = 0) -> dict[str, Any]:
        from jigga.runtime.channels import TelegramAdapter  # shared event mapper

        result = poll_messages(home, long_poll_seconds=long_poll_seconds)
        events = [TelegramAdapter.to_event(m) for m in result.get("messages", [])]
        for event in events:
            event.source = "webchat"
        return {"status": result.get("status", "ok"), "events": events, "raw": result}

    def send(self, home: Path, *, conversation_id: Any, text: str) -> dict[str, Any]:
        return send_message(home, conversation_id, text)

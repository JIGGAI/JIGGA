"""Telegram channel — first Milestone B channel.

A *channel* lets JIGGA receive messages from and reply to an external surface.
Per the project direction, each channel is a **configurable opt-in capability**:
the user enables the ones they want (`jigga capabilities install telegram`).

Telegram is realised as a capability with two actions:

  - `telegram.poll_messages` — inbound. Fetches new messages via the Bot API's
    long-poll `getUpdates`, filters them to an allowlist of chat IDs, normalizes
    them to the common channel-message shape, and advances the read offset.
  - `telegram.send_message` — outbound. Replies via `sendMessage`.

This fits JIGGA's existing model without touching the supervisor: a
cron-triggered workflow polls on a cadence (poll → process → reply), exactly
like the morning-briefing workflow polls calendar/email. Push-based channels
(webhooks) can layer a gateway on top later; polling needs no public endpoint.

## Normalized channel-message shape

`poll_messages` returns a list of dicts in the shape every channel should use,
so workflows handling Telegram / Slack / iMessage look the same:

    {
      "channel": "telegram",
      "chat_id": <int|str>,     # where to reply
      "sender": <str>,          # human-readable sender (username / name)
      "sender_id": <int|str>,   # stable sender id
      "text": <str>,
      "message_id": <int|str>,
      "date": <int>,            # unix ts
    }

## Safety

- **Allowlist by default.** Inbound messages are dropped unless the sender's
  chat ID is in `channels.telegram.allowed_chat_ids`. With no allowlist
  configured, `poll_messages` returns nothing (default-deny) — a bot's token is
  public-reachable, so anyone could DM it. `discover: true` bypasses the filter
  *for setup only* so the user can find their chat ID.
- Channel text is untrusted and prompt-injectable; downstream agents should be
  scoped accordingly (the capability is risk_level: medium).
- All HTTP is in-process `urllib` — native-action category (no subprocess, not
  sandboxed), same as the Google Calendar connector.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from jigga.core.config import load_runtime_config
from jigga.core.io import ensure_dir, read_json, write_json
from jigga.core.models import WorkflowStep
from jigga.runtime.capabilities import CapabilityManifest

API_BASE = "https://api.telegram.org"
BOT_TOKEN_FILENAME = "telegram_bot_token"
STATE_FILENAME = "telegram_state.json"
DEFAULT_TIMEOUT_SECONDS = 30.0
SUPPORTED_ACTIONS = ("telegram.poll_messages", "telegram.send_message")


# --- token + state storage -------------------------------------------------


def bot_token_path(secrets_dir: Path) -> Path:
    return secrets_dir / BOT_TOKEN_FILENAME


def store_bot_token(secrets_dir: Path, token: str) -> Path:
    ensure_dir(secrets_dir)
    path = bot_token_path(secrets_dir)
    path.write_text(token.strip(), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass
    return path


def load_bot_token(secrets_dir: Path) -> str | None:
    path = bot_token_path(secrets_dir)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def _channels_dir(home: Path) -> Path:
    return home / "channels"


def _state_path(home: Path) -> Path:
    return _channels_dir(home) / STATE_FILENAME


def load_offset(home: Path) -> int:
    path = _state_path(home)
    if not path.exists():
        return 0
    try:
        return int(read_json(path).get("offset", 0))
    except (ValueError, OSError):
        return 0


def store_offset(home: Path, offset: int) -> None:
    write_json(_state_path(home), {"offset": int(offset)})


# --- config ----------------------------------------------------------------


def telegram_config(home: Path) -> dict[str, Any]:
    config = load_runtime_config(home)
    channels = config.get("channels") or {}
    return dict(channels.get("telegram") or {})


def allowed_chat_ids(home: Path) -> set[str]:
    raw = telegram_config(home).get("allowed_chat_ids") or []
    return {str(item) for item in raw}


# --- HTTP ------------------------------------------------------------------


def _api_call(
    token: str,
    method: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    url = f"{API_BASE}/bot{token}/{method}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram {method} failed: HTTP {exc.code}: {detail[:400]}") from exc
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} returned not-ok: {body.get('description', body)}")
    return body


# --- normalization ---------------------------------------------------------


def _normalize(update: dict[str, Any]) -> dict[str, Any] | None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return None
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    sender_name = sender.get("username") or sender.get("first_name") or str(sender.get("id", "unknown"))
    # Best-effort "was the bot addressed?" for activation modes: an @mention,
    # text_mention, or /command entity. (We don't resolve which bot; in a
    # one-bot group this is the right signal — documented limitation.)
    entities = message.get("entities") or []
    mentions_bot = any(e.get("type") in {"mention", "text_mention", "bot_command"} for e in entities)
    return {
        "channel": "telegram",
        "chat_id": chat.get("id"),
        "chat_type": chat.get("type"),          # private / group / supergroup / channel
        "sender": sender_name,
        "sender_id": sender.get("id"),
        "text": message.get("text", ""),
        "mentions_bot": mentions_bot,
        "message_id": message.get("message_id"),
        "date": message.get("date"),
        "update_id": update.get("update_id"),
    }


# --- inbound / outbound primitives -----------------------------------------


def poll_messages(
    home: Path,
    *,
    discover: bool = False,
    limit: int = 50,
    long_poll_seconds: int = 0,
) -> dict[str, Any]:
    """Fetch new inbound messages, filter by allowlist, advance the offset.

    Returns {"messages": [...], "status": "ok"} or a not_connected payload.
    `discover=True` bypasses the allowlist (setup-only, to find chat IDs) and
    does NOT advance the offset, so real polling later still sees the messages.

    `long_poll_seconds > 0` enables Telegram long-polling: getUpdates holds the
    connection server-side up to that many seconds waiting for a message
    (efficient — no busy 5s cron). The capability-action path uses 0 (return
    immediately); the channel listener uses a real value (~30).
    """
    secrets_dir = home / "secrets"
    token = load_bot_token(secrets_dir)
    if token is None:
        return _not_connected("telegram.poll_messages")

    offset = load_offset(home)
    # urlopen must outlast the server-side hold, so pad the HTTP timeout.
    http_timeout = DEFAULT_TIMEOUT_SECONDS if long_poll_seconds <= 0 else long_poll_seconds + 10
    body = _api_call(
        token,
        "getUpdates",
        {"offset": offset, "timeout": max(0, long_poll_seconds), "limit": limit},
        timeout=http_timeout,
    )
    updates = body.get("result") or []

    normalized = [m for m in (_normalize(u) for u in updates) if m is not None]

    # Advance offset past everything we fetched (acknowledge), UNLESS we're in
    # discover mode — discovery must not consume messages.
    if updates and not discover:
        highest = max(int(u.get("update_id", 0)) for u in updates)
        store_offset(home, highest + 1)

    if discover:
        return {"channel": "telegram", "status": "ok", "discover": True, "messages": normalized}

    allow = allowed_chat_ids(home)
    if not allow:
        return {
            "channel": "telegram",
            "status": "ok",
            "messages": [],
            "note": (
                "No channels.telegram.allowed_chat_ids configured — dropping all inbound "
                "(default-deny). Run a discover poll or `jigga telegram status` to find your "
                "chat ID, then add it to the allowlist."
            ),
            "dropped": len(normalized),
        }
    kept = [m for m in normalized if str(m.get("chat_id")) in allow]
    return {
        "channel": "telegram",
        "status": "ok",
        "messages": kept,
        "dropped": len(normalized) - len(kept),
    }


def send_message(home: Path, chat_id: Any, text: str) -> dict[str, Any]:
    secrets_dir = home / "secrets"
    token = load_bot_token(secrets_dir)
    if token is None:
        return _not_connected("telegram.send_message")
    body = _api_call(token, "sendMessage", {"chat_id": chat_id, "text": text})
    result = body.get("result") or {}
    # Conversation continuity: every successful outbound lands in the local
    # transcript (the contract: a channel's send primitive records "out").
    # This single funnel covers agent tool replies AND runtime notices
    # (approval prompts, failure replies) — all context the next turn should
    # see. Best-effort: a transcript fault must not break sending.
    try:
        from jigga.runtime.channel_transcript import record
        record(home, "telegram", conversation_id=chat_id, sender="agent",
               text=text, direction="out", message_id=result.get("message_id"))
    except Exception:  # noqa: BLE001
        pass
    return {
        "channel": "telegram",
        "status": "ok",
        "sent": True,
        "chat_id": chat_id,
        "message_id": result.get("message_id"),
    }


def _not_connected(action: str) -> dict[str, Any]:
    return {
        "source": "capability.telegram",
        "action": action,
        "status": "telegram.not_connected",
        "message": (
            "Telegram is not set up. Run `jigga capabilities install telegram` "
            "to add your bot token."
        ),
    }


# --- capability handler ----------------------------------------------------


def telegram_handler(
    step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime,
) -> Any:
    home = runtime.home
    params = resolved_input if isinstance(resolved_input, dict) else {}

    if step.action == "telegram.poll_messages":
        result = poll_messages(home, discover=bool(params.get("discover", False)), limit=int(params.get("limit", 50)))
        result.setdefault("source", "capability.telegram")
        result.setdefault("action", step.action)
        return result
    if step.action == "telegram.send_message":
        chat_id = params.get("chat_id")
        text = params.get("text")
        if chat_id is None or text is None:
            raise ValueError("telegram.send_message requires 'chat_id' and 'text' in input")
        result = send_message(home, chat_id, str(text))
        result.setdefault("source", "capability.telegram")
        result.setdefault("action", step.action)
        return result
    raise ValueError(
        f"Unknown telegram action: {step.action!r}. Supported: {', '.join(SUPPORTED_ACTIONS)}."
    )

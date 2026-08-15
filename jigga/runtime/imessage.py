"""iMessage channel — read the local Messages database, send via AppleScript.

Unlike every other channel, iMessage has no API and no provider. Inbound is a
**read-only query against `~/Library/Messages/chat.db`**, the SQLite database
the Messages app maintains; outbound is `osascript` telling Messages to send.
Both are macOS-only, and both need the user to grant Full Disk Access to
whatever runs JIGGA.

Two consequences shape everything here:

**It degrades, never crashes.** A Linux or Windows install has no chat.db and
no Messages app. `poll` returns a structured `unsupported` status rather than
raising, exactly like the notifications adapter, so a supervisor tick on a Linux
box carrying an iMessage config in its yaml is a no-op and not an outage.

**The database is not ours.** It's opened read-only through a `file:` URI with
`immutable=1` — Messages is a live writer, and taking any lock on a user's
message store to poll it would be indefensible. Every column read defensively:
Apple has changed this schema across releases (`text` went NULL in favour of
`attributedBody`; `destination_caller_id` appeared later), and a missing column
must degrade one field rather than break the channel.

Semantics carry over from the SMS work (FIELD_LESSONS §3.7), because the same
failures apply:

- **Accepted is not delivered.** AppleScript returns once Messages has *taken*
  the message. Whether it left the device, and whether it landed, is not
  knowable from here. `send` reports `accepted` and never claims more.
- **Inbound routes by destination.** A Mac signs in to several handles — a phone
  number and one or more Apple IDs. `destination_caller_id` says which one a
  message arrived at, and each configured handle picks its own agent.
"""

from __future__ import annotations

import platform
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from jigga.core.config import load_runtime_config
from jigga.core.io import ensure_dir, read_json, write_json
from jigga.runtime.audit import append_event

DEFAULT_DB = "~/Library/Messages/chat.db"
# Apple stores message dates as nanoseconds since 2001-01-01 UTC.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
MAX_MESSAGES_PER_POLL = 50


def _config(home: Path) -> dict[str, Any]:
    channels = load_runtime_config(home).get("channels") or {}
    config = channels.get("imessage") if isinstance(channels, dict) else None
    return config if isinstance(config, dict) else {}


def database_path(home: Path) -> Path:
    return Path(str(_config(home).get("database") or DEFAULT_DB)).expanduser()


def handles(home: Path) -> dict[str, dict[str, Any]]:
    """Configured destination handles → their purpose and routing agent."""
    declared = _config(home).get("handles")
    if not isinstance(declared, dict):
        return {}
    return {str(k): (v if isinstance(v, dict) else {}) for k, v in declared.items()}


def availability(home: Path) -> dict[str, Any]:
    """Whether this machine can do iMessage at all, and why not if it can't.

    Separated from `poll` so `jigga doctor` and the setup wizard can ask the
    question without side effects — "it silently did nothing" is the failure
    mode this whole module is written against.
    """
    if platform.system() != "Darwin":
        return {"available": False,
                "reason": f"iMessage needs macOS (this is {platform.system() or 'unknown'})"}
    path = database_path(home)
    if not path.exists():
        return {"available": False, "reason": f"no Messages database at {path}"}
    try:
        _connect(path).close()
    except sqlite3.Error as exc:
        return {"available": False,
                "reason": (f"cannot read {path}: {exc}. Grant Full Disk Access to the "
                           "program running JIGGA (System Settings → Privacy & Security).")}
    if shutil.which("osascript") is None:
        return {"available": False, "reason": "osascript not found; cannot send"}
    return {"available": True, "reason": None}


def _connect(path: Path) -> sqlite3.Connection:
    """Read-only, immutable. Messages is a live writer and this is its file."""
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=5.0)


# --- cursor -----------------------------------------------------------------


def _cursor_path(home: Path) -> Path:
    return Path(home) / "imessage" / "cursor.json"


def read_cursor(home: Path) -> int:
    path = _cursor_path(home)
    if not path.exists():
        return 0
    try:
        return int(read_json(path).get("last_rowid") or 0)
    except (OSError, ValueError, TypeError):
        return 0


def write_cursor(home: Path, last_rowid: int) -> None:
    ensure_dir(_cursor_path(home).parent)
    write_json(_cursor_path(home), {"last_rowid": int(last_rowid)})


# --- reading ----------------------------------------------------------------


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def apple_time(value: Any) -> str | None:
    """Apple's nanosecond-since-2001 stamp → ISO 8601, tolerating the older
    second-resolution form and anything unparseable."""
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    # Post-Sierra values are nanoseconds; earlier ones are seconds.
    seconds = raw / 1_000_000_000 if raw > 10**11 else raw
    try:
        return (APPLE_EPOCH + timedelta(seconds=seconds)).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def fetch_messages(home: Path, *, since_rowid: int | None = None,
                   limit: int = MAX_MESSAGES_PER_POLL) -> list[dict[str, Any]]:
    """Inbound messages newer than the cursor, oldest first.

    Only `is_from_me = 0` — our own sends are already in the transcript and
    replaying them would have the agent answering itself.
    """
    path = database_path(home)
    cursor_at = read_cursor(home) if since_rowid is None else since_rowid
    connection = _connect(path)
    try:
        available = _columns(connection, "message")
        # Selected defensively: Apple has added and removed these across
        # releases, and one missing column must cost one field, not the channel.
        optional = [c for c in ("destination_caller_id", "service", "is_audio_message")
                    if c in available]
        columns = ["message.ROWID", "message.guid", "message.text", "message.date",
                   *(f"message.{c}" for c in optional)]
        query = (
            f"SELECT {', '.join(columns)}, handle.id "  # noqa: S608 — column names are from a fixed allowlist
            "FROM message LEFT JOIN handle ON message.handle_id = handle.ROWID "
            "WHERE message.is_from_me = 0 AND message.ROWID > ? "
            "ORDER BY message.ROWID ASC LIMIT ?"
        )
        rows = connection.execute(query, (cursor_at, int(limit))).fetchall()
    finally:
        connection.close()

    messages: list[dict[str, Any]] = []
    for row in rows:
        values = dict(zip([*columns, "handle.id"], row))
        text = values.get("message.text")
        if not text:
            # Newer macOS stores the body in `attributedBody` (a serialized
            # NSAttributedString). Decoding it needs a parser we don't have, so
            # the message is surfaced as empty rather than silently dropped —
            # a dropped inbound is worse than an obviously empty one.
            text = ""
        messages.append({
            "rowid": values.get("message.ROWID"),
            "guid": values.get("message.guid"),
            "text": text,
            "sender": values.get("handle.id") or "",
            "destination": values.get("message.destination_caller_id") or "",
            "service": values.get("message.service") or "iMessage",
            "at": apple_time(values.get("message.date")),
        })
    return messages


# --- sending ----------------------------------------------------------------


def _escape_applescript(text: str) -> str:
    """Escape for an AppleScript string literal — backslashes first, so the
    escapes added next aren't themselves escaped."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _default_runner(args: list[str]) -> subprocess.CompletedProcess:
    # Not sandboxed, deliberately, and for the same reason as notifications:
    # this is a local-UX tool that needs the user's own session to reach the
    # Messages app. The audit log records the decision either way.
    return subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)


def send_message(home: Path, *, to: str, text: str, service: str = "iMessage",
                 runner: Callable[[list[str]], Any] = _default_runner,
                 logs_dir: Path | None = None) -> dict[str, Any]:
    """Hand a message to Messages.app.

    Returns `accepted` when AppleScript reports success — never `delivered`.
    AppleScript returns once Messages has *taken* the message; whether it left
    the device, and whether it landed, is not observable from here. Saying
    `delivered` would be the precursor stack's mistake with a new spelling.
    """
    script = (
        f'tell application "Messages"\n'
        f'  set targetService to 1st account whose service type = {service}\n'
        f'  set targetBuddy to participant "{_escape_applescript(to)}" of targetService\n'
        f'  send "{_escape_applescript(text)}" to targetBuddy\n'
        f'end tell'
    )
    result = runner(["osascript", "-e", script])
    ok = getattr(result, "returncode", 1) == 0
    record = {
        "status": "accepted" if ok else "failed",
        # Tri-state: None means Messages took it and nobody has told us what
        # happened since. False would assert a non-delivery we don't know either.
        "delivered": None if ok else False,
        "reports_delivery": False,
        "destination": to,
        "service": service,
        "error": None if ok else (getattr(result, "stderr", "") or "").strip()[:400],
    }
    if logs_dir is not None:
        append_event(logs_dir, "imessage.accepted" if ok else "imessage.send_failed",
                     status="ok" if ok else "error", destination=to, service=service,
                     error=record["error"])
    return record


# --- the channel adapter ----------------------------------------------------


class ImessageAdapter:
    """iMessage on the `ChannelAdapter` contract.

    The destination handle is the routing key. A Mac is signed in to several —
    a phone number and one or more Apple IDs — and `destination_caller_id` says
    which one a message arrived at, so a work Apple ID and a personal number can
    reach different agents.
    """

    name = "imessage"
    # Reads a local SQLite file and returns immediately. Claiming to long-poll
    # would hot-spin the supervisor loop.
    long_polls = False
    self_transcribed = False

    def poll(self, home: Path, *, long_poll_seconds: int = 0) -> dict[str, Any]:
        from jigga.runtime.channels import JiggaEvent

        state = availability(home)
        if not state["available"]:
            # Structured, never an exception: a Linux install carrying an
            # iMessage config in its yaml is a no-op, not an outage.
            return {"status": f"unsupported: {state['reason']}", "events": []}
        try:
            messages = fetch_messages(home)
        except sqlite3.Error as exc:
            return {"status": f"error: {exc}", "events": []}

        routes = handles(home)
        events: list[Any] = []
        highest = read_cursor(home)
        for message in messages:
            highest = max(highest, int(message.get("rowid") or 0))
            destination = str(message.get("destination") or "")
            sender = str(message.get("sender") or "")
            route = routes.get(destination, {})
            event = JiggaEvent(
                source="imessage",
                actor={"type": "user", "id": sender, "name": sender},
                # The pair, not the sender: the same person reaching a work
                # Apple ID and a personal number is two conversations.
                conversation={"id": f"{destination}:{sender}" if destination else sender,
                              "type": "private"},
                message={"text": message.get("text") or "", "attachments": []},
                raw={**message, "purpose": route.get("purpose")},
            )
            if route.get("default_agent"):
                event.target = {"agent": str(route["default_agent"])}
            events.append(event)
        # Advance only after building every event: a fault mid-loop should
        # re-deliver rather than silently skip past unread messages.
        if highest > read_cursor(home):
            write_cursor(home, highest)
        return {"status": "ok", "events": events}

    def send(self, home: Path, *, conversation_id: Any, text: str) -> dict[str, Any]:
        """Reply to the handle the message came from, on the account it reached."""
        raw = str(conversation_id)
        _, sep, sender = raw.partition(":")
        destination = sender if sep else raw
        service = str(_config(home).get("service") or "iMessage")
        return send_message(home, to=destination, text=text, service=service,
                            logs_dir=Path(home) / "logs")

    def thread_context(self, home: Path, *, conversation_id: Any,
                       exclude_message_id: Any = None,
                       logs_dir: Path | None = None,
                       agent_id: str | None = None) -> str:
        """Same hook as the other channels — the listener records inbound into
        the shared transcript, so the agent loop needs nothing iMessage-specific."""
        from jigga.runtime.channel_transcript import thread_context_block

        return thread_context_block(home, "imessage", conversation_id,
                                    exclude_message_id=exclude_message_id,
                                    logs_dir=logs_dir, agent_id=agent_id)

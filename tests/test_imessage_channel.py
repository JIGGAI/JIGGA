"""iMessage — reading the Messages database, sending via AppleScript.

The real thing is macOS-only and this suite runs on Linux, so the untestable
surface is kept thin: the query path runs against a **synthetic chat.db with the
real schema**, and the send path goes through an injected runner. What that
can't prove is that Apple's actual schema matches — noted in the PR.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime import imessage as im
from jigga.runtime.channels import ADAPTERS
from jigga.runtime.imessage import (
    ImessageAdapter,
    apple_time,
    availability,
    fetch_messages,
    read_cursor,
    send_message,
    write_cursor,
)

WORK = "work@example.com"
PERSONAL = "+15550000001"
FRIEND = "+15559999999"


def _apple_stamp(when: datetime) -> int:
    """A real Apple date: nanoseconds since 2001-01-01 UTC."""
    return int((when - datetime(2001, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1_000_000_000)


def _chat_db(path: Path, rows: list[dict], *, with_destination: bool = True) -> None:
    """A stand-in for `chat.db` carrying the columns this module reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    destination_column = ", destination_caller_id TEXT" if with_destination else ""
    connection.executescript(f"""
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, service TEXT);
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, date INTEGER,
            handle_id INTEGER, is_from_me INTEGER, service TEXT,
            is_audio_message INTEGER{destination_column}
        );
    """)
    senders: dict[str, int] = {}
    for row in rows:
        sender = row.get("sender") or ""
        if sender and sender not in senders:
            senders[sender] = len(senders) + 1
            connection.execute("INSERT INTO handle (ROWID, id, service) VALUES (?, ?, ?)",
                               (senders[sender], sender, "iMessage"))
        columns = ["guid", "text", "date", "handle_id", "is_from_me", "service", "is_audio_message"]
        values = [row.get("guid", "g"), row.get("text"),
                  row.get("date", _apple_stamp(datetime.now(timezone.utc))),
                  senders.get(sender, 0), row.get("is_from_me", 0), "iMessage", 0]
        if with_destination:
            columns.append("destination_caller_id")
            values.append(row.get("destination"))
        connection.execute(
            f"INSERT INTO message ({', '.join(columns)}) VALUES ({', '.join('?' * len(values))})",
            values)
    connection.commit()
    connection.close()


def _configure(paths, db: Path) -> None:
    config = read_yaml(paths.config)
    channels = dict(config.get("channels") or {})
    channels["imessage"] = {
        "enabled": True,
        "database": str(db),
        "handles": {
            WORK: {"purpose": "client work", "default_agent": "work_lead"},
            PERSONAL: {"purpose": "personal", "default_agent": "assistant"},
        },
    }
    config["channels"] = channels
    write_yaml(paths.config, config)


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


class _Runner:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode, self.stderr, self.calls = returncode, stderr, []

    def __call__(self, args):
        self.calls.append(args)
        return self


# --- registration -----------------------------------------------------------


def test_imessage_is_a_registered_channel() -> None:
    assert "imessage" in ADAPTERS
    adapter = ADAPTERS["imessage"]
    assert adapter.name == "imessage"
    # Reads a local file and returns; claiming to long-poll would hot-spin.
    assert adapter.long_polls is False


# --- it degrades, never crashes ---------------------------------------------


def test_off_macos_it_reports_unsupported_rather_than_raising(tmp_path: Path) -> None:
    """A Linux install carrying an iMessage config is a no-op, not an outage."""
    paths = init_runtime(tmp_path)
    _configure(paths, tmp_path / "chat.db")
    result = ImessageAdapter().poll(paths.home)
    assert result["events"] == []
    assert result["status"].startswith("unsupported")


def test_availability_explains_a_missing_database(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    _configure(paths, tmp_path / "nope.db")
    monkeypatch.setattr(im.platform, "system", lambda: "Darwin")
    state = availability(paths.home)
    assert state["available"] is False
    assert "no Messages database" in state["reason"]


def test_availability_explains_a_permission_problem(tmp_path: Path, monkeypatch) -> None:
    """The overwhelmingly common first-run failure is missing Full Disk Access,
    and the reason has to say so or nobody will guess."""
    db = tmp_path / "chat.db"
    _chat_db(db, [])
    paths = init_runtime(tmp_path)
    _configure(paths, db)
    monkeypatch.setattr(im.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(im, "_connect",
                        lambda _p: (_ for _ in ()).throw(sqlite3.OperationalError("unable to open")))
    state = availability(paths.home)
    assert state["available"] is False
    assert "Full Disk Access" in state["reason"]


def test_availability_needs_osascript_to_send(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "chat.db"
    _chat_db(db, [])
    paths = init_runtime(tmp_path)
    _configure(paths, db)
    monkeypatch.setattr(im.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(im.shutil, "which", lambda _n: None)
    assert "osascript" in availability(paths.home)["reason"]


# --- reading the database ---------------------------------------------------


def test_only_inbound_messages_are_read(tmp_path: Path) -> None:
    """Replaying our own sends would have the agent answering itself."""
    db = tmp_path / "chat.db"
    _chat_db(db, [
        {"text": "hello", "sender": FRIEND, "destination": PERSONAL, "is_from_me": 0},
        {"text": "my own reply", "sender": FRIEND, "destination": PERSONAL, "is_from_me": 1},
    ])
    paths = init_runtime(tmp_path)
    _configure(paths, db)
    assert [m["text"] for m in fetch_messages(paths.home)] == ["hello"]


def test_the_cursor_advances_and_is_not_re_read(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    _chat_db(db, [{"text": "one", "sender": FRIEND, "destination": PERSONAL},
                  {"text": "two", "sender": FRIEND, "destination": PERSONAL}])
    paths = init_runtime(tmp_path)
    _configure(paths, db)
    assert read_cursor(paths.home) == 0
    assert len(fetch_messages(paths.home)) == 2
    write_cursor(paths.home, 2)
    assert fetch_messages(paths.home) == []


def test_a_missing_optional_column_costs_one_field_not_the_channel(tmp_path: Path) -> None:
    """Apple has added and removed these across releases."""
    db = tmp_path / "chat.db"
    _chat_db(db, [{"text": "hi", "sender": FRIEND}], with_destination=False)
    paths = init_runtime(tmp_path)
    _configure(paths, db)
    messages = fetch_messages(paths.home)
    assert [m["text"] for m in messages] == ["hi"]
    assert messages[0]["destination"] == ""       # degraded, not exploded


def test_an_empty_body_is_surfaced_not_dropped(tmp_path: Path) -> None:
    """Newer macOS puts the body in `attributedBody`, which we can't decode. A
    dropped inbound is worse than an obviously empty one."""
    db = tmp_path / "chat.db"
    _chat_db(db, [{"text": None, "sender": FRIEND, "destination": PERSONAL}])
    paths = init_runtime(tmp_path)
    _configure(paths, db)
    messages = fetch_messages(paths.home)
    assert len(messages) == 1
    assert messages[0]["text"] == ""


def test_apple_timestamps_convert(tmp_path: Path) -> None:
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    assert apple_time(_apple_stamp(when)).startswith("2026-08-15T12:00")
    # The pre-Sierra second-resolution form still parses.
    assert apple_time(int((when - datetime(2001, 1, 1, tzinfo=timezone.utc)).total_seconds()))
    for junk in (None, "", 0, -1, "nonsense"):
        assert apple_time(junk) is None


# --- routing by destination handle ------------------------------------------


def test_inbound_routes_by_the_handle_it_arrived_at(tmp_path: Path, monkeypatch) -> None:
    """A Mac is signed in to several handles; a work Apple ID and a personal
    number should reach different agents."""
    db = tmp_path / "chat.db"
    _chat_db(db, [{"text": "about the contract", "sender": FRIEND, "destination": WORK},
                  {"text": "dinner?", "sender": FRIEND, "destination": PERSONAL}])
    paths = init_runtime(tmp_path)
    _configure(paths, db)
    monkeypatch.setattr(im, "availability", lambda _h: {"available": True, "reason": None})
    events = ImessageAdapter().poll(paths.home)["events"]
    assert [e.target["agent"] for e in events] == ["work_lead", "assistant"]
    assert [e.raw["purpose"] for e in events] == ["client work", "personal"]


def test_one_person_on_two_handles_is_two_conversations(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "chat.db"
    _chat_db(db, [{"text": "a", "sender": FRIEND, "destination": WORK},
                  {"text": "b", "sender": FRIEND, "destination": PERSONAL}])
    paths = init_runtime(tmp_path)
    _configure(paths, db)
    monkeypatch.setattr(im, "availability", lambda _h: {"available": True, "reason": None})
    ids = [e.conversation_id for e in ImessageAdapter().poll(paths.home)["events"]]
    assert ids == [f"{WORK}:{FRIEND}", f"{PERSONAL}:{FRIEND}"]


def test_an_unconfigured_handle_still_yields_an_event(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "chat.db"
    _chat_db(db, [{"text": "hello?", "sender": FRIEND, "destination": "other@example.com"}])
    paths = init_runtime(tmp_path)
    _configure(paths, db)
    monkeypatch.setattr(im, "availability", lambda _h: {"available": True, "reason": None})
    events = ImessageAdapter().poll(paths.home)["events"]
    assert len(events) == 1 and events[0].target == {}


def test_polling_advances_the_cursor(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "chat.db"
    _chat_db(db, [{"text": "one", "sender": FRIEND, "destination": PERSONAL}])
    paths = init_runtime(tmp_path)
    _configure(paths, db)
    monkeypatch.setattr(im, "availability", lambda _h: {"available": True, "reason": None})
    adapter = ImessageAdapter()
    assert len(adapter.poll(paths.home)["events"]) == 1
    assert read_cursor(paths.home) == 1
    assert adapter.poll(paths.home)["events"] == []      # not re-delivered


# --- sending ----------------------------------------------------------------


def test_a_send_reports_accepted_never_delivered(tmp_path: Path) -> None:
    """AppleScript returns once Messages has taken it. Whether it left the
    device is not observable from here."""
    paths = init_runtime(tmp_path)
    runner = _Runner()
    result = send_message(paths.home, to=FRIEND, text="on my way",
                          runner=runner, logs_dir=paths.logs)
    assert result["status"] == "accepted"
    assert result["delivered"] is None        # not True, and not False
    assert result["reports_delivery"] is False
    assert any(e["type"] == "imessage.accepted" for e in _events(paths))


def test_a_failed_send_is_an_error_event(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    runner = _Runner(returncode=1, stderr="Messages got an error: not authorized")
    result = send_message(paths.home, to=FRIEND, text="hi", runner=runner, logs_dir=paths.logs)
    assert result["status"] == "failed"
    assert result["delivered"] is False
    assert "not authorized" in result["error"]
    failed = [e for e in _events(paths) if e["type"] == "imessage.send_failed"]
    assert failed and failed[-1]["status"] == "error"


def test_quotes_and_backslashes_are_escaped(tmp_path: Path) -> None:
    """A message body ends up inside an AppleScript string literal."""
    paths = init_runtime(tmp_path)
    runner = _Runner()
    send_message(paths.home, to=FRIEND, text='say "hi" \\ then go', runner=runner)
    script = runner.calls[0][-1]
    assert '\\"hi\\"' in script
    assert "\\\\" in script


def test_the_adapter_replies_to_the_sender_not_our_own_handle(tmp_path: Path, monkeypatch) -> None:
    """Conversation ids are `<destination>:<sender>`. Replying to the
    destination would send the message to ourselves."""
    paths = init_runtime(tmp_path)
    _configure(paths, tmp_path / "chat.db")
    sent: dict = {}
    monkeypatch.setattr(im, "send_message",
                        lambda home, *, to, text, service, logs_dir=None:
                        sent.update(to=to, text=text) or {"status": "accepted"})
    ImessageAdapter().send(paths.home, conversation_id=f"{WORK}:{FRIEND}", text="yes")
    assert sent["to"] == FRIEND

    # A bare handle (no destination recorded) still addresses the sender.
    ImessageAdapter().send(paths.home, conversation_id=FRIEND, text="yes")
    assert sent["to"] == FRIEND


def test_doctor_warns_when_imessage_cannot_run_here(tmp_path: Path) -> None:
    """Enabled on a machine that can't possibly run it. Silently polling
    nothing forever is the failure this catches."""
    from jigga.runtime import doctor

    paths = init_runtime(tmp_path)
    _configure(paths, tmp_path / "chat.db")
    check = doctor._check_channels(paths)
    assert check.status == doctor.WARN
    assert "iMessage can't run here" in check.detail
    assert "macOS" in check.detail

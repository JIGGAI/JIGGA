"""email-imap capability: filter mapping, search/get over a fake IMAP client,
file-first drafts, gated send over a fake SMTP client, and the setup wizard."""

from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.runtime import email_imap as mod


def _connect(paths) -> None:
    mod.store_credentials(paths.home, {
        "imap_host": "imap.test", "imap_port": 993,
        "smtp_host": "smtp.test", "smtp_port": 465, "smtp_security": "ssl",
        "username": "me@test", "password": "app-pass", "from_address": "me@test",
    })


_RAW_MESSAGE = (b"From: Alice <alice@test>\r\nTo: me@test\r\nSubject: Hello\r\n"
                b"Date: Mon, 27 Jul 2026 10:00:00 +0000\r\n"
                b"Content-Type: text/plain\r\n\r\nLunch tomorrow?")


class _FakeImap:
    def __init__(self) -> None:
        self.selected = None
        self.searched: list[tuple] = []
        self.logged_out = False

    def select(self, folder, readonly=False):
        self.selected = folder
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            self.searched.append(args)
            return "OK", [b"101 102"]
        if command == "FETCH":
            return "OK", [(b"101 (BODY[] {1})", _RAW_MESSAGE)]
        raise AssertionError(command)

    def logout(self):
        self.logged_out = True


class _FakeSmtp:
    def __init__(self) -> None:
        self.sent = []
        self.quit_called = False

    def send_message(self, message):
        self.sent.append(message)

    def quit(self):
        self.quit_called = True


def test_filter_mapping() -> None:
    criteria = mod._imap_criteria(["unread", "important", "from:alice@test", "subject:lunch", "budget"])
    assert criteria[:2] == ["UNSEEN", "FLAGGED"]
    assert ["FROM", "alice@test"] == criteria[2:4]
    assert ["SUBJECT", "lunch"] == criteria[4:6]
    assert ["TEXT", "budget"] == criteria[6:8]
    assert mod._imap_criteria(None) == ["ALL"]
    assert mod._imap_criteria(["today"])[0] == "SINCE"


def test_search_requires_connection(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    with pytest.raises(ValueError, match="capabilities install email-imap"):
        mod.email_search(paths.home, ["unread"])


def test_search_returns_headers_newest_first(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _connect(paths)
    fake = _FakeImap()
    monkeypatch.setattr(mod, "_imap_connect", lambda creds: fake)
    result = mod.email_search(paths.home, ["unread"], limit=5)
    assert fake.selected == "INBOX" and fake.logged_out
    assert result["count"] == 2
    assert result["messages"][0]["uid"] == "102"  # newest first
    assert result["messages"][0]["subject"] == "Hello"


def test_get_reads_plain_body(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _connect(paths)
    monkeypatch.setattr(mod, "_imap_connect", lambda creds: _FakeImap())
    message = mod.email_get(paths.home, "101")
    assert message["from"] == "Alice <alice@test>"
    assert message["body"] == "Lunch tomorrow?"
    assert message["truncated"] is False


def test_draft_is_file_first_and_send_marks_sent(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _connect(paths)
    draft = mod.email_draft(paths.home, to="alice@test", subject="Re: Hello", body="Yes!")
    on_disk = paths.home / "email" / "drafts" / f"{draft['id']}.json"
    assert on_disk.exists()

    fake = _FakeSmtp()
    monkeypatch.setattr(mod, "_smtp_connect", lambda creds: fake)
    result = mod.email_send(paths.home, draft_id=draft["id"])
    assert result["sent"] is True and fake.quit_called
    assert fake.sent[0]["To"] == "alice@test" and fake.sent[0]["From"] == "me@test"
    # Draft marked sent; re-send refuses.
    with pytest.raises(ValueError, match="already sent"):
        mod.email_send(paths.home, draft_id=draft["id"])


def test_send_direct_requires_to_and_subject(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _connect(paths)
    with pytest.raises(ValueError, match="draft_id or input.to"):
        mod.email_send(paths.home, body="no recipient")


def test_capability_registered_medium_risk_and_gated(tmp_path: Path) -> None:
    from jigga.runtime.capabilities import CapabilityRegistry, load_capability_manifest, record_approval
    from jigga.optional_capabilities import REGISTRY

    paths = init_runtime(tmp_path, examples=True)
    manifest_path = REGISTRY["email-imap"].manifest_path
    # Simulate `jigga capabilities install email-imap`: copy pack into user dir + approve.
    pack_dir = paths.capabilities / "email-imap"
    pack_dir.mkdir(parents=True)
    (pack_dir / "manifest.yaml").write_text(manifest_path.read_text(encoding="utf-8"), encoding="utf-8")
    record_approval(paths.policies, load_capability_manifest(pack_dir / "manifest.yaml"))
    registry = CapabilityRegistry.load(user_capabilities=paths.capabilities, approvals_dir=paths.policies)
    capability = registry.resolve_action("email.send")
    assert capability is not None and capability.name == "email-imap"
    assert capability.risk_level == "medium"
    # User-installed pack takes precedence over the bundled dry-run email stub.
    assert registry.resolve_action("email.search").name == "email-imap"


def test_setup_wizard_stores_credentials_0600(tmp_path: Path) -> None:
    from jigga.optional_capabilities.email import setup

    paths = init_runtime(tmp_path, examples=True)
    answers = iter(["imap.test", "smtp.test", "me@test", "app-pass", "", "587", "starttls", ""])
    assert setup(paths, input_fn=lambda _p: next(answers), print_fn=lambda *a, **k: None) == 0
    creds = mod.load_credentials(paths.home)
    assert creds["smtp_port"] == 587 and creds["smtp_security"] == "starttls"
    assert creds["from_address"] == "me@test"
    assert (mod.secrets_path(paths.home).stat().st_mode & 0o777) == 0o600


def test_setup_wizard_aborts_without_required_fields(tmp_path: Path) -> None:
    from jigga.optional_capabilities.email import setup

    paths = init_runtime(tmp_path, examples=True)
    answers = iter(["", "smtp.test", "me@test", "pass"])
    assert setup(paths, input_fn=lambda _p: next(answers), print_fn=lambda *a, **k: None) == 1
    assert not mod.secrets_path(paths.home).exists()

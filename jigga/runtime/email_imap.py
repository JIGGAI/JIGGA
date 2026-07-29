"""Provider-agnostic email — IMAP read + SMTP draft/send (the last Milestone A
connector gap; Gmail/Workspace users already have `gog`).

Opt-in first-party capability (`jigga capabilities install email-imap`), BYO
credentials: host/port/username/app-password collected by the setup wizard into
`~/.jigga/secrets/email_imap.json` (0600). stdlib only (imaplib / smtplib /
email).

Actions:
- `email.search` — friendly filters (`unread`, `today`, `from:x`, `subject:x`,
  free text) mapped to IMAP criteria; returns headers, newest first.
- `email.get` — one message's plain-text body (truncated).
- `email.draft` — file-first: drafts land in `~/.jigga/email/drafts/<id>.json`
  for review; nothing leaves the machine.
- `email.send` — SMTP send of a draft (`draft_id`) or direct to/subject/body.
  The capability is `risk_level: medium`, so outside autonomous mode every
  call — send above all — parks for approval.

Inbound email is untrusted, prompt-injectable content; scope agents that read
mail accordingly (same warning as channels).
"""

from __future__ import annotations

import email
import email.header
import imaplib
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, read_json, write_json
from jigga.core.models import now_iso
from jigga.runtime.audit import new_id

_SECRETS_FILE = "email_imap.json"
_BODY_LIMIT = 8_000
_DEFAULT_SEARCH_LIMIT = 10


def secrets_path(home: Path) -> Path:
    return Path(home) / "secrets" / _SECRETS_FILE


def store_credentials(home: Path, creds: dict[str, Any]) -> Path:
    # E1a: routed through the secrets broker (same on-disk file; one chokepoint).
    import json

    from jigga.runtime.secrets_broker import set_secret

    return Path(set_secret(home, _SECRETS_FILE, json.dumps(creds, indent=1)))


def load_credentials(home: Path) -> dict[str, Any]:
    import json

    from jigga.runtime.secrets_broker import get_secret

    value = get_secret(home, _SECRETS_FILE)
    if value is None:
        raise ValueError(
            "Email is not connected. Run: jigga capabilities install email-imap"
        )
    return json.loads(value)


def _imap_connect(creds: dict[str, Any]) -> imaplib.IMAP4:
    client = imaplib.IMAP4_SSL(creds["imap_host"], int(creds.get("imap_port") or 993))
    client.login(creds["username"], creds["password"])
    return client


def _smtp_connect(creds: dict[str, Any]) -> smtplib.SMTP:
    host = creds["smtp_host"]
    port = int(creds.get("smtp_port") or 465)
    if str(creds.get("smtp_security", "ssl")).lower() == "starttls":
        client = smtplib.SMTP(host, port, timeout=30)
        client.starttls()
    else:
        client = smtplib.SMTP_SSL(host, port, timeout=30)
    client.login(creds["username"], creds["password"])
    return client


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for chunk, charset in email.header.decode_header(value):
        parts.append(chunk.decode(charset or "utf-8", errors="replace")
                     if isinstance(chunk, bytes) else chunk)
    return "".join(parts)


def _imap_criteria(filters: Any) -> list[str]:
    """Map friendly filters to IMAP SEARCH criteria. Unknown plain terms become
    full-text TEXT searches; explicit prefixes cover the common asks."""
    if isinstance(filters, str):
        filters = [filters]
    criteria: list[str] = []
    for item in filters or []:
        term = str(item).strip()
        lowered = term.lower()
        if not term:
            continue
        if lowered == "unread":
            criteria.append("UNSEEN")
        elif lowered == "important":
            criteria.append("FLAGGED")
        elif lowered == "today":
            criteria += ["SINCE", datetime.now(timezone.utc).strftime("%d-%b-%Y")]
        elif lowered.startswith("from:"):
            criteria += ["FROM", term[5:].strip()]
        elif lowered.startswith("subject:"):
            criteria += ["SUBJECT", term[8:].strip()]
        else:
            criteria += ["TEXT", term]
    return criteria or ["ALL"]


def _plain_body(message: email.message.Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        return "(no plain-text part)"
    payload = message.get_payload(decode=True)
    if payload is None:
        return str(message.get_payload())
    return payload.decode(message.get_content_charset() or "utf-8", errors="replace")


def email_search(home: Path, filters: Any = None, *, folder: str = "INBOX",
                 limit: int = _DEFAULT_SEARCH_LIMIT) -> dict[str, Any]:
    creds = load_credentials(home)
    client = _imap_connect(creds)
    try:
        client.select(folder, readonly=True)
        _status, data = client.uid("SEARCH", None, *_imap_criteria(filters))
        uids = (data[0].split() if data and data[0] else [])[-limit:][::-1]  # newest first
        messages = []
        for uid in uids:
            _status, fetched = client.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            raw = next((part[1] for part in fetched or [] if isinstance(part, tuple)), b"")
            headers = email.message_from_bytes(raw)
            messages.append({
                "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                "from": _decode_header(headers.get("From")),
                "subject": _decode_header(headers.get("Subject")),
                "date": headers.get("Date", ""),
            })
        return {"source": "capability.email_imap", "folder": folder,
                "count": len(messages), "messages": messages}
    finally:
        client.logout()


def email_get(home: Path, uid: str, *, folder: str = "INBOX") -> dict[str, Any]:
    if not uid:
        raise ValueError("email.get requires input.uid")
    creds = load_credentials(home)
    client = _imap_connect(creds)
    try:
        client.select(folder, readonly=True)
        _status, fetched = client.uid("FETCH", str(uid).encode(), "(BODY.PEEK[])")
        raw = next((part[1] for part in fetched or [] if isinstance(part, tuple)), None)
        if raw is None:
            raise ValueError(f"Message not found: uid {uid} in {folder}")
        message = email.message_from_bytes(raw)
        body = _plain_body(message)
        return {
            "source": "capability.email_imap", "uid": str(uid),
            "from": _decode_header(message.get("From")),
            "to": _decode_header(message.get("To")),
            "subject": _decode_header(message.get("Subject")),
            "date": message.get("Date", ""),
            "body": body[:_BODY_LIMIT],
            "truncated": len(body) > _BODY_LIMIT,
        }
    finally:
        client.logout()


def _drafts_dir(home: Path) -> Path:
    return Path(home) / "email" / "drafts"


def email_draft(home: Path, *, to: str, subject: str, body: str) -> dict[str, Any]:
    if not to or not subject:
        raise ValueError("email.draft requires input.to and input.subject")
    draft = {
        "id": new_id("draft"), "to": to, "subject": subject, "body": body or "",
        "created_at": now_iso(), "status": "draft", "sent_at": None,
    }
    ensure_dir(_drafts_dir(home))
    write_json(_drafts_dir(home) / f"{draft['id']}.json", draft)
    return {"source": "capability.email_imap", **draft}


def list_drafts(home: Path) -> list[dict[str, Any]]:
    drafts_dir = _drafts_dir(home)
    records = []
    for path in sorted(drafts_dir.glob("*.json")) if drafts_dir.exists() else []:
        try:
            records.append(read_json(path))
        except (OSError, ValueError):
            continue
    return records


def email_send(home: Path, *, draft_id: str | None = None, to: str | None = None,
               subject: str | None = None, body: str | None = None) -> dict[str, Any]:
    creds = load_credentials(home)
    draft = None
    if draft_id:
        draft_path = _drafts_dir(home) / f"{draft_id}.json"
        if not draft_path.exists():
            raise ValueError(f"Draft not found: {draft_id}")
        draft = read_json(draft_path)
        if draft.get("status") == "sent":
            raise ValueError(f"Draft already sent: {draft_id}")
        to, subject, body = draft["to"], draft["subject"], draft.get("body", "")
    if not to or not subject:
        raise ValueError("email.send requires a draft_id or input.to + input.subject")

    message = EmailMessage()
    message["From"] = creds.get("from_address") or creds["username"]
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body or "")
    client = _smtp_connect(creds)
    try:
        client.send_message(message)
    finally:
        client.quit()
    if draft is not None:
        draft["status"] = "sent"
        draft["sent_at"] = now_iso()
        write_json(_drafts_dir(home) / f"{draft_id}.json", draft)
    return {"source": "capability.email_imap", "sent": True, "to": to,
            "subject": subject, "draft_id": draft_id}


def email_imap_handler(step, _capability, resolved_input, _memory_context, runtime) -> Any:
    data = resolved_input if isinstance(resolved_input, dict) else {}
    home = runtime.home
    if step.action == "email.search":
        return email_search(home, data.get("filters") or data.get("query"),
                            folder=str(data.get("folder") or "INBOX"),
                            limit=int(data.get("limit") or _DEFAULT_SEARCH_LIMIT))
    if step.action == "email.get":
        return email_get(home, str(data.get("uid") or ""), folder=str(data.get("folder") or "INBOX"))
    if step.action == "email.draft":
        return email_draft(home, to=str(data.get("to") or ""),
                           subject=str(data.get("subject") or ""), body=str(data.get("body") or ""))
    if step.action == "email.send":
        return email_send(home, draft_id=data.get("draft_id"), to=data.get("to"),
                          subject=data.get("subject"), body=data.get("body"))
    raise ValueError(f"Unknown email action: {step.action}")


def status(home: Path) -> dict[str, Any]:
    connected = secrets_path(home).exists()
    result: dict[str, Any] = {"connected": connected}
    if connected:
        creds = load_credentials(home)
        result.update({k: creds.get(k) for k in ("imap_host", "smtp_host", "username", "from_address")})
        result["drafts"] = len(list_drafts(home))
    return result


def logout(home: Path) -> bool:
    path = secrets_path(home)
    if path.exists():
        path.unlink()
        return True
    return False

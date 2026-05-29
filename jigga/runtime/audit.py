from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir
from jigga.core.models import now_iso

REDACTED = "***redacted***"

# Detail keys whose values are scrubbed regardless of content.
_SENSITIVE_KEYS = (
    "token", "password", "secret", "api_key", "apikey", "authorization",
    "credential", "credentials", "access_token", "refresh_token", "bot_token",
    "client_secret", "private_key",
)

# Value patterns scrubbed wherever they appear (a token echoed inside an error
# string, a bearer header, etc.). Conservative — these shapes don't occur in
# ordinary prose.
_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),                 # OpenAI-style keys
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),            # GitHub tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),          # Slack tokens
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                      # AWS access key id
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"),             # Telegram bot token
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{16,}\b"),      # Bearer <token>
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _scrub_str(value: str) -> str:
    scrubbed = value
    for pattern in _VALUE_PATTERNS:
        scrubbed = pattern.sub(REDACTED, scrubbed)
    return scrubbed


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively scrub secrets from audit detail values.

    Two layers: any value under a sensitive key name is dropped entirely, and
    any string (anywhere) has known secret shapes pattern-replaced. Audit logs
    are durable and user-inspectable, so this is a defensive net against a
    capability echoing a credential into an error/detail field.
    """
    if key is not None and any(token in key.lower() for token in _SENSITIVE_KEYS):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _scrub_str(value)
    return value


def append_event(logs_dir: Path, event_type: str, status: str = "ok", **details: Any) -> dict[str, Any]:
    ensure_dir(logs_dir)
    event = {
        "id": new_id("evt"),
        "time": now_iso(),
        "type": event_type,
        "status": status,
        "details": {key: redact(value, key=key) for key, value in details.items()},
    }
    with (logs_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return event

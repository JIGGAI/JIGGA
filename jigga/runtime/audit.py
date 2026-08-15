from __future__ import annotations

import contextlib
import contextvars
import json
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir
from jigga.core.models import now_iso

REDACTED = "***redacted***"

# Ambient trace id for the current logical operation. A supervisor tick, the
# agent runs it wakes, the workflow steps they execute, and any subagents they
# spawn all run on one thread in one process, so a ContextVar threads a single
# trace_id through every `append_event` without each call site passing it. Set
# at the entry points (supervisor_tick / run_agent / run_workflow / ingest_once)
# via `trace_context`; nested entries inherit the active id rather than mint a
# new one, so `jigga trace <id>` returns the whole causal tree from one id.
_current_trace: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "jigga_trace_id", default=None
)

# Who is acting. On the precursor stack 22 posts vanished and it was
# permanently unanswerable who deleted them: the automation wrote through the
# same API as the humans, and `created_by` was the constant `dashboard-ui` for
# every row. The only forensic tool left was diffing hourly SQLite snapshots.
#
# Format is a prefixed label so machine and human separate on a prefix match:
#   user            a person, at the CLI
#   user:<channel>  a person, over a channel they messaged from
#   agent:<id>      an agent's own turn
#   workflow:<id>   a workflow run executing its steps
#   supervisor      the heartbeat acting on no one's direct instruction
#   system          unattributed (the default, and a bug wherever it shows up
#                   on a mutation)
#
# Unlike the trace id, the INNERMOST binding wins: a supervisor tick that wakes
# an agent attributes that agent's actions to the agent, because that is who
# performed them. What a human initiated is recoverable from the trace root,
# which carries `user` — the two questions are "who did this" and "what set it
# off", and they have different answers.
ACTOR_USER = "user"
ACTOR_SYSTEM = "system"
ACTOR_SUPERVISOR = "supervisor"

_current_actor: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "jigga_actor", default=None
)


def current_actor() -> str:
    return _current_actor.get() or ACTOR_SYSTEM


def is_human(actor: str | None = None) -> bool:
    """True when the actor is a person rather than something JIGGA ran itself.
    The distinction the precursor stack could not make."""
    resolved = actor if actor is not None else current_actor()
    return resolved == ACTOR_USER or resolved.startswith(f"{ACTOR_USER}:")

# Detail keys whose values are scrubbed regardless of content. Matched at word
# boundaries (see `_SENSITIVE_KEY_PATTERNS`) so `token` redacts `bot_token` /
# `access_token` but not the non-secret `input_tokens` / `output_tokens`.
_SENSITIVE_KEYS = (
    "token", "password", "secret", "api_key", "apikey", "authorization",
    "credential", "credentials", "access_token", "refresh_token", "bot_token",
    "client_secret", "private_key",
)

# A term is sensitive when it appears delimited by string ends or a non
# alphanumeric char — so `tokens` (a count) is not mistaken for `token`.
_SENSITIVE_KEY_PATTERNS = tuple(
    re.compile(rf"(?:^|[^a-z0-9]){re.escape(term)}(?:[^a-z0-9]|$)")
    for term in _SENSITIVE_KEYS
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


def current_trace_id() -> str | None:
    """The trace id bound to the current operation, or None outside one."""
    return _current_trace.get()


@contextlib.contextmanager
def trace_context(trace_id: str | None = None) -> Iterator[str]:
    """Bind an ambient trace id for the duration of a logical operation.

    An explicit `trace_id` wins; otherwise an already-active trace is inherited
    (so nested entry points join their parent's trace) and only a top-level
    entry with no active trace mints a fresh one. Every `append_event` emitted
    inside the block carries this id under `details.trace_id`.
    """
    resolved = trace_id or current_trace_id() or new_id("trace")
    token = _current_trace.set(resolved)
    try:
        yield resolved
    finally:
        _current_trace.reset(token)


@contextlib.contextmanager
def actor_context(actor: str) -> Iterator[str]:
    """Bind who is acting for the duration of a block.

    Innermost wins, deliberately — see the note on `_current_actor`. Every
    `append_event` inside the block carries this under `actor`.
    """
    token = _current_actor.set(actor)
    try:
        yield actor
    finally:
        _current_actor.reset(token)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(pattern.search(lowered) for pattern in _SENSITIVE_KEY_PATTERNS)


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
    if key is not None and _is_sensitive_key(key):
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
    scrubbed = {key: redact(value, key=key) for key, value in details.items()}
    trace_id = _current_trace.get()
    if trace_id is not None and "trace_id" not in scrubbed:
        scrubbed["trace_id"] = trace_id
    event = {
        "id": new_id("evt"),
        "time": now_iso(),
        "type": event_type,
        "status": status,
        # Top-level, not inside details: attribution is a property of the event
        # itself, and `jigga audit --actor` must not have to reach into a bag
        # that individual call sites control.
        "actor": current_actor(),
        "details": scrubbed,
    }
    with (logs_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return event

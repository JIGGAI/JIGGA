"""The webhook listener — JIGGA's first inbound network surface.

It does exactly two things: authenticate, and enqueue. Everything else was
deliberately pushed elsewhere — targeting is enforced by the queue drain
(`supervisor._drain_event_queue`, a workflow must declare a matching
`trigger.webhook`), and execution happens on the heartbeat inside the tick
budget. Keeping the listener this thin is the point: it is the piece exposed to
the network, so it should contain as little logic as possible.

Security posture, in the order it matters:

**Off by default.** Adding a listening socket to someone's machine is not a
thing to do implicitly. `webhook.enabled` must be set.

**No key, no listener.** If no API key is configured the server refuses to
start rather than serving unauthenticated — a listener that silently accepted
anonymous requests because a secret was missing is the failure this ordering
prevents. Fail closed, loudly.

**Constant-time comparison.** A plain `==` on a bearer token leaks its prefix
through timing.

**Bound body.** An unbounded read is a trivial memory exhaustion.

**Localhost by default.** Binding 0.0.0.0 is opt-in and stated in config, never
a default someone inherits without deciding.

Nothing here trusts the payload: it is written to the queue as data and only
ever reaches a workflow as `${trigger.*}` references, which fail closed on a
typo (#181).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from jigga.core.config import load_runtime_config
from jigga.core.paths import JiggaPaths
from jigga.runtime.audit import append_event
from jigga.runtime.event_queue import QueueFull, enqueue

DEFAULT_PORT = 8899
DEFAULT_BIND = "127.0.0.1"
# 64 KiB. Comfortably larger than any real webhook payload, small enough that a
# hostile sender cannot exhaust memory.
DEFAULT_MAX_BODY_BYTES = 64 * 1024
SECRET_NAME = "webhook_api_key"
# Per-caller keys live at `webhook_key@<source>`. JIGGA is the provider: it
# issues one credential per third party, so a caller can be named in the audit
# log and revoked without touching the others.
ISSUED_KEY_PREFIX = "webhook_key"
# The identity recorded when a request authenticates with the legacy single
# shared key. Named, rather than blank, so "we don't know which caller" is
# visible in the audit log instead of looking like an ordinary source.
SHARED_CALLER = "shared"


class WebhookNotConfigured(RuntimeError):
    """The listener cannot start safely as configured."""


def settings(home: Any) -> dict[str, Any]:
    return (load_runtime_config(home).get("webhook") or {})


def is_enabled(home: Any) -> bool:
    return bool(settings(home).get("enabled"))


def _int_setting(home: Any, key: str, default: int) -> int:
    try:
        return int(settings(home).get(key, default))
    except (TypeError, ValueError):
        return default


def api_key(paths: JiggaPaths) -> str | None:
    """The shared bearer key, if one is stored.

    Retained as a fallback so an install predating per-source keys keeps
    working. New installs should issue one key per caller — see `issued_keys`.
    """
    from jigga.runtime.secrets_broker import get_secret

    value = get_secret(paths.home, SECRET_NAME)
    return value.strip() if value else None


def issued_keys(paths: JiggaPaths) -> dict[str, str]:
    """Every per-caller key, as `{source: key}`.

    JIGGA is the *provider* here: it issues a credential to each third party
    that wants to invoke a hook. Issuing one credential to everybody would mean
    no way to say which caller authenticated, no way to revoke one without
    breaking the rest, and a leak anywhere compromising every integration — so
    keys are stored per source, `webhook_key@<source>`, reusing the `@`
    convention team-scoped secrets already established.
    """
    from jigga.runtime.secrets_broker import get_secret, list_secrets

    prefix = f"{ISSUED_KEY_PREFIX}@"
    keys: dict[str, str] = {}
    for name in list_secrets(paths.home):
        if not name.startswith(prefix):
            continue
        source = name[len(prefix):]
        value = get_secret(paths.home, name)
        if source and value and value.strip():
            keys[source] = value.strip()
    return keys


def identify_caller(paths_or_keys: Any, presented: str, shared: str | None = None) -> str | None:
    """Which caller this bearer token belongs to, or None if it matches nothing.

    Every comparison is constant-time, and *all* of them run — returning early
    on the first match would leak, through timing, roughly where in the list a
    given key sits.
    """
    keys = paths_or_keys if isinstance(paths_or_keys, dict) else issued_keys(paths_or_keys)
    matched: str | None = None
    for source, key in sorted(keys.items()):
        if hmac.compare_digest(presented, key):
            matched = source
    if shared and hmac.compare_digest(presented, shared):
        matched = matched or SHARED_CALLER
    return matched


def _idempotency_key(headers: Any, body: bytes) -> str:
    """Prefer the sender's own delivery id; fall back to a hash of the body.

    The fallback matters: a provider that retries without a delivery header
    would otherwise be undeduplicable, and delivery is at-least-once for all of
    them.
    """
    for header in ("X-Idempotency-Key", "X-Delivery-Id", "X-GitHub-Delivery", "X-Request-Id"):
        value = headers.get(header)
        if value:
            return str(value)[:200]
    return "sha256:" + hashlib.sha256(body).hexdigest()


class _Handler(BaseHTTPRequestHandler):
    server_version = "jigga"
    sys_version = ""          # don't advertise the Python version

    paths: JiggaPaths
    expected_key: str | None       # legacy shared key, if one is stored
    issued: dict[str, str]         # per-caller keys, {source: key}
    max_body: int

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        """Silence stdlib access logging — the audit log is the record, and
        stdout on a daemon is where things go to be lost."""

    def _reply(self, code: int, body: dict[str, Any], extra_headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _caller(self) -> str | None:
        """The authenticated caller's name, or None.

        Returns *who*, not merely whether: a provider issuing credentials needs
        to be able to say which third party made a call, revoke one without
        breaking the others, and let a workflow accept only its own sender.
        """
        header = self.headers.get("Authorization") or ""
        prefix = "Bearer "
        if not header.startswith(prefix):
            return None
        return identify_caller(self.issued, header[len(prefix):].strip(), self.expected_key)

    def do_POST(self) -> None:  # noqa: N802 — stdlib naming
        kind = self.path.strip("/").split("/")[-1] if self.path.startswith("/hooks/") else ""
        if not self.path.startswith("/hooks/") or not kind:
            self._reply(404, {"error": "not found"})
            return

        caller = self._caller()
        if caller is None:
            # Audited without the presented value: recording a guessed key would
            # write someone's near-miss credential into the log.
            append_event(self.paths.logs, "webhook.unauthorized", status="denied",
                         kind=kind, remote=self.client_address[0])
            self._reply(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            self._reply(411, {"error": "content-length required"})
            return
        if length > self.max_body:
            append_event(self.paths.logs, "webhook.rejected", status="error", kind=kind,
                         reason="body too large", bytes=length)
            self._reply(413, {"error": "payload too large", "max_bytes": self.max_body})
            return

        body = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            self._reply(400, {"error": "body must be JSON"})
            return
        if not isinstance(payload, dict):
            self._reply(400, {"error": "body must be a JSON object"})
            return

        try:
            # `source` is the AUTHENTICATED caller, never anything the request
            # claimed about itself — it is what the drain checks a workflow's
            # `source:` against.
            result = enqueue(self.paths, source=caller, kind=kind, payload=payload,
                             idempotency_key=_idempotency_key(self.headers, body))
        except QueueFull:
            # Retryable, explicitly: the sender must be told to come back rather
            # than believing a dropped event landed.
            self._reply(503, {"error": "queue full, retry later"}, {"Retry-After": "60"})
            return
        except Exception as exc:  # noqa: BLE001 — never leak an internal error to the network
            append_event(self.paths.logs, "webhook.error", status="error", kind=kind,
                         source=caller, error=str(exc))
            self._reply(500, {"error": "internal error"})
            return

        if result.get("status") == "duplicate":
            # 200, not 202: nothing new was accepted, and a provider replaying a
            # delivery should see success rather than retrying forever.
            self._reply(200, {"status": "duplicate"})
            return
        self._reply(202, {"status": "accepted", "id": result.get("id")})


def build_server(paths: JiggaPaths) -> ThreadingHTTPServer:
    """Construct the listener, or refuse to.

    Raises `WebhookNotConfigured` when disabled or when no API key is stored —
    starting an unauthenticated listener because a secret happened to be
    missing is exactly the failure worth being loud about.
    """
    if not is_enabled(paths.home):
        raise WebhookNotConfigured("webhook listener is disabled (set `webhook.enabled: true`)")
    shared = api_key(paths)
    issued = issued_keys(paths)
    if not shared and not issued:
        raise WebhookNotConfigured(
            f"no webhook keys issued — run `jigga webhook issue <caller>` to mint one per third "
            f"party (or `jigga secrets set {SECRET_NAME}` for a single shared key). "
            "The listener will not start unauthenticated.")

    bind = str(settings(paths.home).get("bind", DEFAULT_BIND))
    port = _int_setting(paths.home, "port", DEFAULT_PORT)
    handler = type("_BoundHandler", (_Handler,), {
        "paths": paths,
        "expected_key": shared,
        "issued": issued,
        "max_body": _int_setting(paths.home, "max_body_bytes", DEFAULT_MAX_BODY_BYTES),
    })
    server = ThreadingHTTPServer((bind, port), handler)
    server.daemon_threads = True
    return server


def serve_in_background(paths: JiggaPaths) -> tuple[ThreadingHTTPServer, threading.Thread] | None:
    """Start the listener on a background thread. None when not configured.

    Failing to start is reported and non-fatal: the supervisor's job is running
    agents, and it must keep doing that whether or not the optional listener
    came up.
    """
    try:
        server = build_server(paths)
    except WebhookNotConfigured as exc:
        if is_enabled(paths.home):   # enabled but unusable is worth an event
            append_event(paths.logs, "webhook.not_started", status="error", error=str(exc))
        return None
    except OSError as exc:           # port in use, permission denied
        append_event(paths.logs, "webhook.not_started", status="error", error=str(exc))
        return None

    thread = threading.Thread(target=server.serve_forever, name="jigga-webhook", daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    append_event(paths.logs, "webhook.listening", bind=str(host), port=port,
                 callers=sorted(issued_keys(paths)),
                 shared_key=bool(api_key(paths)))
    return server, thread

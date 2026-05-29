"""Google Calendar capability — real OAuth + Calendar API integration.

This is the first connector that talks to a real third-party service on the
user's behalf. It implements:

  - OAuth 2.0 authorization code flow with PKCE (Proof Key for Code Exchange),
    using a loopback HTTP redirect (http://localhost:RANDOM_PORT). This is the
    Google-recommended pattern for "Desktop app" OAuth clients.
  - Token persistence at `~/.jigga/secrets/google_calendar_tokens.json`
    with 0600 file permissions.
  - Automatic access-token refresh when the stored token is past its
    expiration timestamp.
  - Two API calls covering the bundled `google-calendar` capability's
    actions: `events.list` (`google_calendar.list_events`) and
    `events.get` (`google_calendar.get_event`).
  - A capability handler dispatched by `step.action` that returns clear
    "not connected" output when no client config or tokens are present —
    workflows degrade gracefully rather than crashing.

Setup pattern (user-facing):

  1. Create a "Desktop app" OAuth client in Google Cloud Console.
  2. Download the client config JSON.
  3. `jigga calendar setup <path/to/downloaded.json>` — copies it into
     `~/.jigga/secrets/google_calendar_client.json` (0600).
  4. `jigga calendar login` — opens browser, completes PKCE flow,
     persists tokens.
  5. Workflow steps using `google_calendar.list_events` /
     `google_calendar.get_event` now hit the real API.

Per the subprocess routing rule:
  - The OAuth flow uses `webbrowser.open()` (render side — needs user's
    browser env), which is fine to invoke directly.
  - All API calls are pure HTTP via stdlib `urllib.request`. No subprocess.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets as _stdlib_secrets
import shutil
import socketserver
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir
from jigga.core.models import WorkflowStep
from jigga.runtime.capabilities import CapabilityManifest

# --- Constants --------------------------------------------------------------

OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

CLIENT_CONFIG_FILENAME = "google_calendar_client.json"
TOKENS_FILENAME = "google_calendar_tokens.json"

# Refresh access tokens this many seconds before their stated expiry to avoid
# racing the API call.
TOKEN_REFRESH_LEEWAY_SECONDS = 60

# Default loopback flow timeout — long enough for the user to authenticate
# in their browser, short enough that a forgotten flow times out gracefully.
LOOPBACK_TIMEOUT_SECONDS = 300


# --- Data shapes ------------------------------------------------------------


@dataclass(frozen=True)
class OAuthClientConfig:
    """The JSON Google Cloud Console hands you for a Desktop OAuth client."""

    client_id: str
    client_secret: str
    redirect_uris: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OAuthClientConfig":
        # Google ships the client JSON under one of two top-level keys:
        # "installed" (Desktop app) or "web" (Web app). We require the
        # Desktop shape because the loopback flow only works for installed
        # clients without a pre-registered redirect URL.
        body = data.get("installed") or data.get("web") or data
        client_id = body.get("client_id")
        client_secret = body.get("client_secret")
        if not client_id or not client_secret:
            raise ValueError(
                "Google OAuth client config is missing client_id or client_secret. "
                "Download the 'Desktop app' JSON from Google Cloud Console → "
                "APIs & Services → Credentials → OAuth 2.0 Client IDs."
            )
        return cls(
            client_id=str(client_id),
            client_secret=str(client_secret),
            redirect_uris=[str(uri) for uri in body.get("redirect_uris", []) or []],
        )


@dataclass
class TokenSet:
    """OAuth tokens persisted between runs."""

    access_token: str
    refresh_token: str
    expires_at: str
    token_type: str = "Bearer"
    scope: str = CALENDAR_READONLY_SCOPE

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
            "scope": self.scope,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TokenSet":
        return cls(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            expires_at=str(data["expires_at"]),
            token_type=str(data.get("token_type") or "Bearer"),
            scope=str(data.get("scope") or CALENDAR_READONLY_SCOPE),
        )

    def is_expired(self, now: datetime | None = None) -> bool:
        try:
            expiry = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return True
        current = now or datetime.now(timezone.utc)
        return current >= expiry - timedelta(seconds=TOKEN_REFRESH_LEEWAY_SECONDS)


# --- File I/O for the secrets dir ------------------------------------------


def client_config_path(secrets_dir: Path) -> Path:
    return secrets_dir / CLIENT_CONFIG_FILENAME


def tokens_path(secrets_dir: Path) -> Path:
    return secrets_dir / TOKENS_FILENAME


def load_client_config(secrets_dir: Path) -> OAuthClientConfig | None:
    path = client_config_path(secrets_dir)
    if not path.exists():
        return None
    return OAuthClientConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))


def store_client_config(secrets_dir: Path, source_path: Path) -> Path:
    """Copy a user-supplied OAuth client JSON into the secrets dir."""
    ensure_dir(secrets_dir)
    target = client_config_path(secrets_dir)
    # Validate the source by parsing it before copying — bad files shouldn't
    # land in the secrets dir.
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    OAuthClientConfig.from_dict(payload)  # raises ValueError on bad shape
    shutil.copyfile(source_path, target)
    _set_owner_only(target)
    return target


def load_tokens(secrets_dir: Path) -> TokenSet | None:
    path = tokens_path(secrets_dir)
    if not path.exists():
        return None
    return TokenSet.from_dict(json.loads(path.read_text(encoding="utf-8")))


def store_tokens(secrets_dir: Path, tokens: TokenSet) -> Path:
    ensure_dir(secrets_dir)
    path = tokens_path(secrets_dir)
    path.write_text(json.dumps(tokens.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    _set_owner_only(path)
    return path


def delete_tokens(secrets_dir: Path) -> bool:
    path = tokens_path(secrets_dir)
    if not path.exists():
        return False
    path.unlink()
    return True


def _set_owner_only(path: Path) -> None:
    """Owner-only file permissions (0600). Best-effort — Windows and some
    filesystems don't support POSIX modes; caller doesn't need to react."""
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


# --- PKCE helpers ----------------------------------------------------------


def generate_pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge) for PKCE per RFC 7636. The verifier is a
    high-entropy random string the client retains; the challenge is its
    SHA-256 hash sent on the authorize URL. Google rejects PKCE flows that
    use the verifier as the challenge (`plain` method), so we always use
    `S256`."""
    verifier = _stdlib_secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return verifier, challenge


# --- OAuth flow (interactive — uses the user's browser) --------------------


def build_authorize_url(client: OAuthClientConfig, port: int, state: str, challenge: str) -> str:
    return f"{OAUTH_AUTH_URL}?" + urllib.parse.urlencode(
        {
            "client_id": client.client_id,
            "redirect_uri": f"http://localhost:{port}",
            "response_type": "code",
            "scope": CALENDAR_READONLY_SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # offline + consent forces Google to return a refresh_token, even
            # on re-auth — without these we'd only get an access_token.
            "access_type": "offline",
            "prompt": "consent",
        }
    )


def exchange_code_for_tokens(
    client: OAuthClientConfig,
    code: str,
    verifier: str,
    port: int,
) -> TokenSet:
    """Exchange an OAuth authorization code for access + refresh tokens."""
    payload = urllib.parse.urlencode(
        {
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": f"http://localhost:{port}",
        }
    ).encode("utf-8")
    return _post_token_request(payload)


def refresh_access_token(client: OAuthClientConfig, tokens: TokenSet) -> TokenSet:
    """Use the refresh_token to get a new access_token. Refresh tokens stay
    valid across this exchange; the new token set keeps the same refresh_token
    unless Google rotates it."""
    payload = urllib.parse.urlencode(
        {
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "refresh_token": tokens.refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    new_tokens = _post_token_request(payload, fallback_refresh_token=tokens.refresh_token)
    return new_tokens


def _post_token_request(payload: bytes, fallback_refresh_token: str | None = None) -> TokenSet:
    request = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OAuth token request failed: HTTP {exc.code}: {detail[:500]}") from exc
    access = body.get("access_token")
    if not access:
        raise RuntimeError(f"OAuth token response missing access_token: {body}")
    expires_in = int(body.get("expires_in") or 3600)
    refresh = body.get("refresh_token") or fallback_refresh_token
    if not refresh:
        raise RuntimeError(
            "OAuth token response missing refresh_token. Re-run `jigga calendar login` "
            "with prompt=consent to obtain one."
        )
    return TokenSet(
        access_token=str(access),
        refresh_token=str(refresh),
        expires_at=(datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat(),
        token_type=str(body.get("token_type") or "Bearer"),
        scope=str(body.get("scope") or CALENDAR_READONLY_SCOPE),
    )


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Loopback HTTP handler that captures the authorization code Google
    redirects to. Stores result on the server instance so the outer flow can
    pick it up after `handle_request()` returns."""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        self.server.received_code = qs.get("code", [None])[0]  # type: ignore[attr-defined]
        self.server.received_state = qs.get("state", [None])[0]  # type: ignore[attr-defined]
        self.server.received_error = qs.get("error", [None])[0]  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        outcome = (
            "Authorization complete. You can close this window."
            if self.server.received_code  # type: ignore[attr-defined]
            else f"Authorization failed: {self.server.received_error or 'unknown error'}"  # type: ignore[attr-defined]
        )
        self.wfile.write(f"<html><body><h2>JIGGA: {outcome}</h2></body></html>".encode("utf-8"))

    def log_message(self, *args: Any) -> None:  # noqa: A002
        # Suppress the default per-request stderr logging.
        pass


def run_oauth_flow(
    secrets_dir: Path,
    *,
    open_browser: bool = True,
    timeout_seconds: float = LOOPBACK_TIMEOUT_SECONDS,
) -> TokenSet:
    """Run the full OAuth authorization code + PKCE loopback flow.

    Blocks until Google redirects to the loopback server (success or error).
    The capability handler does NOT call this — only the `jigga calendar login`
    CLI command does. Handlers only use the stored tokens.
    """
    client = load_client_config(secrets_dir)
    if client is None:
        raise FileNotFoundError(
            f"No Google OAuth client config at {client_config_path(secrets_dir)}. "
            "Run `jigga calendar setup <path/to/client_secret.json>` first."
        )

    state = _stdlib_secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()

    server = socketserver.TCPServer(("127.0.0.1", 0), _CallbackHandler)
    server.received_code = None  # type: ignore[attr-defined]
    server.received_state = None  # type: ignore[attr-defined]
    server.received_error = None  # type: ignore[attr-defined]
    port = server.server_address[1]
    server.timeout = timeout_seconds

    url = build_authorize_url(client, port, state, challenge)
    if open_browser:
        webbrowser.open(url)

    try:
        server.handle_request()  # blocks until one request (success or timeout)
    finally:
        server.server_close()

    if getattr(server, "received_error", None):
        raise RuntimeError(f"OAuth authorization failed: {server.received_error}")  # type: ignore[attr-defined]
    if getattr(server, "received_state", None) != state:
        raise RuntimeError("OAuth state mismatch — possible CSRF or stale flow")
    code = getattr(server, "received_code", None)
    if not code:
        raise RuntimeError(
            "OAuth flow timed out waiting for the loopback redirect. Re-run "
            "`jigga calendar login` and complete the consent screen."
        )

    tokens = exchange_code_for_tokens(client, code, verifier, port)
    store_tokens(secrets_dir, tokens)
    return tokens


# --- Token management used by the capability handler -----------------------


def get_valid_tokens(secrets_dir: Path) -> TokenSet | None:
    """Return a TokenSet that's safe to use immediately. Refreshes if the
    stored access token is expired. Returns None if no tokens exist (caller
    should report the "not connected" path)."""
    tokens = load_tokens(secrets_dir)
    if tokens is None:
        return None
    if not tokens.is_expired():
        return tokens
    client = load_client_config(secrets_dir)
    if client is None:
        return None
    refreshed = refresh_access_token(client, tokens)
    store_tokens(secrets_dir, refreshed)
    return refreshed


# --- API client (events.list / events.get) ---------------------------------


def list_events(
    tokens: TokenSet,
    *,
    calendar_id: str = "primary",
    time_min: datetime | None = None,
    time_max: datetime | None = None,
    max_results: int = 25,
    single_events: bool = True,
) -> dict[str, Any]:
    """Call calendar.events.list. Defaults to "events between now and end of
    today in user's primary calendar, expanded to single instances ordered by
    start time" — matches what a morning briefing wants."""
    if time_min is None:
        time_min = datetime.now(timezone.utc)
    if time_max is None:
        end_of_today = time_min.replace(hour=23, minute=59, second=59, microsecond=0)
        time_max = end_of_today
    params: dict[str, str] = {
        "timeMin": _to_iso_utc(time_min),
        "timeMax": _to_iso_utc(time_max),
        "singleEvents": "true" if single_events else "false",
        "maxResults": str(max(1, min(int(max_results), 250))),
    }
    if single_events:
        params["orderBy"] = "startTime"
    url = (
        f"{CALENDAR_API_BASE}/calendars/"
        f"{urllib.parse.quote(calendar_id)}/events?"
        f"{urllib.parse.urlencode(params)}"
    )
    return _api_get(tokens, url)


def get_event(
    tokens: TokenSet,
    *,
    event_id: str,
    calendar_id: str = "primary",
) -> dict[str, Any]:
    """Call calendar.events.get for a specific event."""
    if not event_id:
        raise ValueError("get_event requires a non-empty event_id")
    url = (
        f"{CALENDAR_API_BASE}/calendars/"
        f"{urllib.parse.quote(calendar_id)}/events/"
        f"{urllib.parse.quote(event_id)}"
    )
    return _api_get(tokens, url)


def _api_get(tokens: TokenSet, url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"{tokens.token_type} {tokens.access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Google Calendar API request failed: HTTP {exc.code}: {detail[:500]}"
        ) from exc


def _to_iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Workflow event normalization ------------------------------------------


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    """Flatten the verbose Google event payload into the shape the dry-run
    `calendar.list_events` handler returns: {time, title, source, ...}. Lets
    downstream workflow steps (notification body coercion, summarization) work
    against either the dry-run or real provider without per-source branching.
    """
    start = event.get("start") or {}
    when = start.get("dateTime") or start.get("date") or ""
    return {
        "id": event.get("id"),
        "title": event.get("summary") or "(no title)",
        "time": when,
        "location": event.get("location"),
        "attendees": [
            {"email": a.get("email"), "response": a.get("responseStatus")}
            for a in event.get("attendees") or []
        ],
        "url": event.get("hangoutLink") or event.get("htmlLink"),
        "source": "capability.google_calendar",
    }


# --- Capability handler ----------------------------------------------------


_NOT_CONNECTED = "google-calendar.not_connected"


def _not_connected_response(action: str, message: str) -> dict[str, Any]:
    return {
        "source": "capability.google_calendar",
        "action": action,
        "delivered": False,
        "events": [] if action == "google_calendar.list_events" else None,
        "status": _NOT_CONNECTED,
        "message": message,
    }


def google_calendar_handler(
    step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime,
) -> Any:
    """Dispatch by step.action against the real Google Calendar API.

    Gracefully degrades when no client config or tokens are present — workflow
    runs without OAuth setup get a structured "not_connected" payload rather
    than a crash, so workflows can branch on `status` if needed.
    """
    secrets_dir = runtime.home / "secrets"
    client = load_client_config(secrets_dir)
    if client is None:
        return _not_connected_response(
            step.action,
            "Google Calendar is not installed. "
            "Run `jigga capabilities install google-calendar` to set it up.",
        )

    tokens = get_valid_tokens(secrets_dir)
    if tokens is None:
        return _not_connected_response(
            step.action,
            "Google Calendar is not logged in. Run `jigga calendar login`.",
        )

    if step.action == "google_calendar.list_events":
        return _list_events_action(tokens, resolved_input)
    if step.action == "google_calendar.get_event":
        return _get_event_action(tokens, resolved_input)
    raise ValueError(
        f"Unknown google-calendar action: {step.action!r}. "
        "Supported: google_calendar.list_events, google_calendar.get_event."
    )


def _list_events_action(tokens: TokenSet, resolved_input: Any) -> dict[str, Any]:
    params = resolved_input if isinstance(resolved_input, dict) else {}
    raw = list_events(
        tokens,
        calendar_id=str(params.get("calendar_id") or "primary"),
        time_min=_parse_optional_dt(params.get("time_min")),
        time_max=_parse_optional_dt(params.get("time_max")),
        max_results=int(params.get("max_results") or 25),
        single_events=bool(params.get("single_events", True)),
    )
    events = [normalize_event(item) for item in (raw.get("items") or [])]
    return {
        "source": "capability.google_calendar",
        "action": "google_calendar.list_events",
        "calendar_id": str(params.get("calendar_id") or "primary"),
        "count": len(events),
        "events": events,
        "next_page_token": raw.get("nextPageToken"),
        "status": "ok",
    }


def _get_event_action(tokens: TokenSet, resolved_input: Any) -> dict[str, Any]:
    params = resolved_input if isinstance(resolved_input, dict) else {}
    event_id = str(params.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("google_calendar.get_event requires 'event_id' in input")
    raw = get_event(
        tokens,
        event_id=event_id,
        calendar_id=str(params.get("calendar_id") or "primary"),
    )
    return {
        "source": "capability.google_calendar",
        "action": "google_calendar.get_event",
        "event_id": event_id,
        "event": normalize_event(raw),
        "raw": raw,
        "status": "ok",
    }


def _parse_optional_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None

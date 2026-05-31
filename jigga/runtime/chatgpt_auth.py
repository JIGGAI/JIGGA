"""ChatGPT-subscription credentials for the `chatgpt_oauth` model provider.

This lets JIGGA run models on a ChatGPT Plus/Pro subscription (flat-rate, no
per-token API billing) the way the Codex CLI and openclaw do — by holding an
OAuth access token for `chatgpt.com/backend-api` rather than an API key.

JIGGA keeps its **own** credential store at `<home>/secrets/chatgpt_auth.json`
(written by `chatgpt_login`). When that's absent it falls back to importing the
token the Codex CLI stores at `~/.codex/auth.json`, so an existing `codex login`
just works. Tokens are refreshed here when expired and written back to whichever
store they came from.

Verified against codex 0.135.0 and the live backend:
- client id `app_EMoamEEZ73f0CkXaXp7hrann`, endpoints under `auth.openai.com`
- store shape `{tokens: {access_token, refresh_token, id_token, account_id}, ...}`
- `account_id` lives in the access-token JWT claim `https://api.openai.com/auth`
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

from jigga.core.io import write_json

# Codex's public OAuth client id (reused by openclaw and the community plugins).
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/oauth/authorize"
TOKEN_URL = f"{ISSUER}/oauth/token"
# Allow-listed for the Codex client; we reuse it for the browser-paste flow.
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"
ACCOUNT_CLAIM = "https://api.openai.com/auth"
# Refresh a little before the JWT actually expires to avoid a mid-call 401.
EXPIRY_SKEW_SECONDS = 300

STORE_FILENAME = "chatgpt_auth.json"


class ChatGptAuthError(RuntimeError):
    pass


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()


def codex_store() -> Path:
    return codex_home() / "auth.json"


def jigga_store(home: Path) -> Path:
    return Path(home) / "secrets" / STORE_FILENAME


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Best-effort decode of a JWT payload (no signature check — we only read
    public claims like `exp` and the account id)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # pad base64url
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, IndexError, json.JSONDecodeError):
        return {}


def account_id_from(access_token: str, fallback: str | None = None) -> str | None:
    claims = decode_jwt_claims(access_token).get(ACCOUNT_CLAIM) or {}
    return claims.get("chatgpt_account_id") or fallback


def _is_expired(access_token: str, *, now: float | None = None) -> bool:
    exp = decode_jwt_claims(access_token).get("exp")
    if not isinstance(exp, (int, float)):
        return False  # can't tell → assume valid, let a 401 trigger refresh
    return (now or time.time()) >= exp - EXPIRY_SKEW_SECONDS


def _refresh(refresh_token: str) -> dict[str, Any]:
    body = json.dumps(
        {"client_id": CLIENT_ID, "grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — surface a clear auth error
        raise ChatGptAuthError(f"Token refresh failed: {exc}") from exc


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def save_credentials(home: Path, tokens: dict[str, Any]) -> Path:
    """Write a fresh login into JIGGA's own store (0600). `tokens` carries
    access_token / refresh_token / id_token / account_id."""
    path = jigga_store(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"tokens": tokens, "last_refresh": _now_iso()})
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


class ChatGptCredentials:
    """An access token + account id, refreshed on demand against its store."""

    def __init__(self, access_token: str, account_id: str | None, *, path: Path, raw: dict[str, Any]):
        self.access_token = access_token
        self.account_id = account_id
        self._path = path
        self._raw = raw

    def force_refresh(self) -> "ChatGptCredentials":
        """Refresh after a 401, persisting the rotated tokens back to the store."""
        tokens = self._raw.get("tokens") or {}
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise ChatGptAuthError("No refresh_token available to refresh ChatGPT credentials.")
        refreshed = _refresh(refresh_token)
        tokens["access_token"] = refreshed.get("access_token") or tokens.get("access_token")
        if refreshed.get("refresh_token"):
            tokens["refresh_token"] = refreshed["refresh_token"]
        if refreshed.get("id_token"):
            tokens["id_token"] = refreshed["id_token"]
        tokens["account_id"] = account_id_from(tokens["access_token"], tokens.get("account_id"))
        self._raw["tokens"] = tokens
        self._raw["last_refresh"] = _now_iso()
        write_json(self._path, self._raw)
        self.access_token = tokens["access_token"]
        self.account_id = tokens.get("account_id")
        return self


def _candidate_stores(home: Path | None) -> list[Path]:
    stores: list[Path] = []
    if home is not None:
        stores.append(jigga_store(home))  # JIGGA's own store wins
    stores.append(codex_store())           # fall back to an existing `codex login`
    return stores


def credential_source(home: Path | None = None) -> Path | None:
    """The store a login would load from, or None if not logged in anywhere."""
    return next((p for p in _candidate_stores(home) if p.exists()), None)


def login_state(home: Path | None = None) -> dict[str, Any]:
    path = credential_source(home)
    if path is None:
        return {"logged_in": False, "source": None, "account_id": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        access = (raw.get("tokens") or {}).get("access_token", "")
    except (OSError, json.JSONDecodeError):
        return {"logged_in": False, "source": str(path), "account_id": None}
    source = "jigga" if home is not None and path == jigga_store(home) else "codex"
    return {"logged_in": bool(access), "source": source, "path": str(path),
            "account_id": account_id_from(access)}


def load_credentials(*, home: Path | None = None, now: float | None = None) -> ChatGptCredentials:
    """Load ChatGPT credentials — JIGGA's own store first, else codex's —
    refreshing if the access token is expired. Raises ChatGptAuthError when no
    usable login exists."""
    path = credential_source(home)
    if path is None:
        looked = ", ".join(str(p) for p in _candidate_stores(home))
        raise ChatGptAuthError(
            f"No ChatGPT login found (looked in {looked}). "
            f"Run `jigga model login` (or `codex login`) first."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ChatGptAuthError(f"Malformed auth store at {path}: {exc}") from exc
    tokens = raw.get("tokens") or {}
    access = tokens.get("access_token")
    if not access:
        raise ChatGptAuthError(f"No access_token in {path}; re-run `jigga model login`.")
    creds = ChatGptCredentials(
        access, account_id_from(access, tokens.get("account_id")), path=path, raw=raw
    )
    if _is_expired(access, now=now):
        creds.force_refresh()
    return creds

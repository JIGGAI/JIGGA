"""ChatGPT-subscription credentials for the `chatgpt_oauth` model provider.

This lets JIGGA run models on a ChatGPT Plus/Pro subscription (flat-rate, no
per-token API billing) the way the Codex CLI and openclaw do — by holding an
OAuth access token for `chatgpt.com/backend-api` rather than an API key.

For now JIGGA **reads the credentials the Codex CLI already stores** in
`~/.codex/auth.json` (`codex login` does the browser OAuth). JIGGA's own login
flow — browser-paste and device-code — lands in a follow-up; this module is the
shared credential layer both will use. Tokens are refreshed here when expired
and written back to the same store so JIGGA and codex stay in sync.

Verified against codex 0.135.0 and the live backend:
- client id `app_EMoamEEZ73f0CkXaXp7hrann`, token endpoint `auth.openai.com/oauth/token`
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
TOKEN_URL = "https://auth.openai.com/oauth/token"
ACCOUNT_CLAIM = "https://api.openai.com/auth"
# Refresh a little before the JWT actually expires to avoid a mid-call 401.
EXPIRY_SKEW_SECONDS = 300


class ChatGptAuthError(RuntimeError):
    pass


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser()


def auth_file(home: Path | None = None) -> Path:
    return (home or codex_home()) / "auth.json"


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """Best-effort decode of a JWT payload (no signature check — we only read
    public claims like `exp` and the account id)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # pad base64url
        return json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, IndexError, json.JSONDecodeError):
        return {}


def _account_id_from(access_token: str, fallback: str | None) -> str | None:
    claims = _decode_jwt_claims(access_token).get(ACCOUNT_CLAIM) or {}
    return claims.get("chatgpt_account_id") or fallback


def _is_expired(access_token: str, *, now: float | None = None) -> bool:
    exp = _decode_jwt_claims(access_token).get("exp")
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


def _persist(path: Path, data: dict[str, Any], tokens: dict[str, Any]) -> None:
    data["tokens"] = tokens
    data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    write_json(path, data)


class ChatGptCredentials:
    """An access token + account id, refreshed on demand."""

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
        tokens["account_id"] = _account_id_from(tokens["access_token"], tokens.get("account_id"))
        _persist(self._path, self._raw, tokens)
        self.access_token = tokens["access_token"]
        self.account_id = tokens.get("account_id")
        return self


def load_credentials(*, home: Path | None = None, now: float | None = None) -> ChatGptCredentials:
    """Load ChatGPT credentials from the codex store, refreshing if the access
    token is expired. Raises ChatGptAuthError when no usable login exists."""
    path = auth_file(home)
    if not path.exists():
        raise ChatGptAuthError(
            f"No ChatGPT login found at {path}. Run `codex login` (or JIGGA's auth flow) first."
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ChatGptAuthError(f"Malformed auth store at {path}: {exc}") from exc
    tokens = raw.get("tokens") or {}
    access = tokens.get("access_token")
    if not access:
        raise ChatGptAuthError(f"No access_token in {path}; re-run `codex login`.")
    creds = ChatGptCredentials(
        access, _account_id_from(access, tokens.get("account_id")), path=path, raw=raw
    )
    if _is_expired(access, now=now):
        creds.force_refresh()
    return creds

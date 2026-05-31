"""JIGGA-native ChatGPT login — so a fresh install can authenticate without the
Codex CLI. Two flows, both PKCE against `auth.openai.com` using the Codex client:

- **browser-paste** (default, headless-friendly): JIGGA prints the authorize URL;
  you sign in; OpenAI redirects to `localhost:1455/auth/callback?code=…`; you
  paste that return URL back; JIGGA exchanges the code for tokens.
- **device-code**: JIGGA shows a short code + `auth.openai.com/codex/device`,
  polls until you approve, then exchanges the server-issued code.

Tokens land in JIGGA's own store via `chatgpt_auth.save_credentials`. Endpoints,
params, and the device sub-API are taken from the openai/codex source.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from jigga.runtime.chatgpt_auth import (
    AUTHORIZE_URL,
    CLIENT_ID,
    ISSUER,
    REDIRECT_URI,
    SCOPE,
    TOKEN_URL,
    ChatGptAuthError,
    account_id_from,
    save_credentials,
)

_DEVICE_USERCODE_URL = f"{ISSUER}/api/accounts/deviceauth/usercode"
_DEVICE_TOKEN_URL = f"{ISSUER}/api/accounts/deviceauth/token"
_DEVICE_VERIFY_URL = f"{ISSUER}/codex/device"
_DEVICE_REDIRECT_URI = f"{ISSUER}/deviceauth/callback"
_ORIGINATOR = "codex_cli_rs"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def generate_pkce() -> tuple[str, str]:
    """Return (verifier, challenge) for PKCE S256."""
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def build_authorize_url(*, redirect_uri: str, state: str, challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": _ORIGINATOR,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def parse_redirect_input(text: str) -> tuple[str, str | None]:
    """Extract (code, state) from whatever the user pastes — a full
    `localhost:1455/...?code=…&state=…` URL, a bare `?code=…` query, or just the
    raw code."""
    text = text.strip()
    if not text:
        raise ChatGptAuthError("Nothing pasted.")
    if "code=" in text:
        query = text.split("?", 1)[1] if "?" in text else text
        parsed = urllib.parse.parse_qs(query)
        code = (parsed.get("code") or [""])[0]
        state = (parsed.get("state") or [None])[0]
        if not code:
            raise ChatGptAuthError("No `code` found in the pasted URL.")
        return code, state
    if "?" in text or "://" in text:  # looks like a URL but carries no code
        raise ChatGptAuthError("No `code` found in the pasted URL.")
    return text, None  # assume the user pasted just the code


def _post(url: str, *, json_body: dict[str, Any] | None = None, form: dict[str, Any] | None = None) -> dict[str, Any]:
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        content_type = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(json_body or {}).encode()
        content_type = "application/json"
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": content_type, "User-Agent": "codex_cli_rs/0.135.0"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8")), response.status


def exchange_code(*, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
    """Exchange an authorization code for tokens (form-encoded, per codex)."""
    body, _ = _post(TOKEN_URL, form={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": CLIENT_ID, "code_verifier": code_verifier,
    })
    return body


def tokens_payload(exchanged: dict[str, Any]) -> dict[str, Any]:
    """Normalize a token-endpoint response into our stored shape."""
    access = exchanged.get("access_token") or ""
    return {
        "access_token": access,
        "refresh_token": exchanged.get("refresh_token"),
        "id_token": exchanged.get("id_token"),
        "account_id": account_id_from(access),
    }


# --- browser-paste flow ----------------------------------------------------


def browser_login(
    home: Path, *, prompt: Callable[[str], str] = input, echo: Callable[[str], None] = print
) -> dict[str, Any]:
    verifier, challenge = generate_pkce()
    state = _b64url(secrets.token_bytes(16))
    url = build_authorize_url(redirect_uri=REDIRECT_URI, state=state, challenge=challenge)
    echo("\nOpen this URL in your browser and sign in with your ChatGPT account:\n")
    echo(f"  {url}\n")
    echo("After approving you'll be redirected to a localhost URL that won't load — that's fine.")
    pasted = prompt("Paste the full redirect URL (or just the code) here: ")
    code, returned_state = parse_redirect_input(pasted)
    if returned_state is not None and returned_state != state:
        raise ChatGptAuthError("State mismatch — the pasted URL doesn't match this login attempt.")
    tokens = tokens_payload(exchange_code(code=code, code_verifier=verifier, redirect_uri=REDIRECT_URI))
    if not tokens["access_token"]:
        raise ChatGptAuthError("Token exchange returned no access_token.")
    save_credentials(home, tokens)
    return {"account_id": tokens["account_id"]}


# --- device-code flow ------------------------------------------------------


def request_device_code() -> dict[str, Any]:
    body, _ = _post(_DEVICE_USERCODE_URL, json_body={"client_id": CLIENT_ID})
    return body


def poll_device(device_auth_id: str, user_code: str) -> dict[str, Any] | None:
    """One poll. Returns the exchange material when approved, or None while
    still pending."""
    request = urllib.request.Request(
        _DEVICE_TOKEN_URL,
        data=json.dumps({"device_auth_id": device_auth_id, "user_code": user_code}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "codex_cli_rs/0.135.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404, 428):  # still pending
            return None
        raise ChatGptAuthError(f"Device authorization failed: HTTP {exc.code}") from exc


def device_login(
    home: Path, *, echo: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep, max_seconds: int = 900,
) -> dict[str, Any]:
    start = request_device_code()
    device_auth_id = start.get("device_auth_id")
    user_code = start.get("user_code")
    interval = float(start.get("interval") or 5)
    if not device_auth_id or not user_code:
        raise ChatGptAuthError("Device authorization did not return a user code.")
    echo(f"\nGo to {_DEVICE_VERIFY_URL} and enter this code:\n\n    {user_code}\n")
    echo("Waiting for approval...")
    waited = 0.0
    while waited < max_seconds:
        result = poll_device(device_auth_id, user_code)
        if result is not None:
            tokens = tokens_payload(exchange_code(
                code=result["authorization_code"], code_verifier=result["code_verifier"],
                redirect_uri=_DEVICE_REDIRECT_URI,
            ))
            if not tokens["access_token"]:
                raise ChatGptAuthError("Token exchange returned no access_token.")
            save_credentials(home, tokens)
            return {"account_id": tokens["account_id"]}
        sleep(interval)
        waited += interval
    raise ChatGptAuthError("Device login timed out waiting for approval.")

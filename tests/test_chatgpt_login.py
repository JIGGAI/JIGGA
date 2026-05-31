from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path

import pytest

from jigga.runtime import chatgpt_login
from jigga.runtime.chatgpt_auth import (
    ChatGptAuthError,
    jigga_store,
    load_credentials,
    login_state,
    save_credentials,
)
from jigga.runtime.chatgpt_login import (
    build_authorize_url,
    device_login,
    browser_login,
    generate_pkce,
    parse_redirect_input,
    tokens_payload,
)


def _jwt(claims: dict) -> str:
    def b64(o: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


def _access(account="acct_1", exp_in=3600) -> str:
    return _jwt({"exp": int(time.time()) + exp_in, "https://api.openai.com/auth": {"chatgpt_account_id": account}})


# --- PKCE + URL + parsing --------------------------------------------------


def test_generate_pkce_is_s256() -> None:
    verifier, challenge = generate_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected


def test_build_authorize_url_has_required_params() -> None:
    url = build_authorize_url(redirect_uri="http://localhost:1455/auth/callback", state="st", challenge="ch")
    for frag in ("response_type=code", "code_challenge=ch", "code_challenge_method=S256", "state=st",
                 "client_id=app_EMoamEEZ73f0CkXaXp7hrann"):
        assert frag in url


def test_parse_redirect_input_variants() -> None:
    assert parse_redirect_input("http://localhost:1455/auth/callback?code=abc&state=xyz") == ("abc", "xyz")
    assert parse_redirect_input("?code=abc&state=xyz") == ("abc", "xyz")
    assert parse_redirect_input("rawcode") == ("rawcode", None)
    with pytest.raises(ChatGptAuthError):
        parse_redirect_input("")
    with pytest.raises(ChatGptAuthError):
        parse_redirect_input("http://localhost/cb?error=denied")


def test_tokens_payload_extracts_account_id() -> None:
    out = tokens_payload({"access_token": _access("acct_9"), "refresh_token": "r", "id_token": "i"})
    assert out["account_id"] == "acct_9" and out["refresh_token"] == "r"


# --- browser-paste flow ----------------------------------------------------


def test_browser_login_exchanges_and_saves(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(chatgpt_login, "exchange_code",
                        lambda **kw: {"access_token": _access("acct_b"), "refresh_token": "r1"})
    result = browser_login(tmp_path, prompt=lambda _: "the-code", echo=lambda _m: None)
    assert result["account_id"] == "acct_b"
    saved = json.loads(jigga_store(tmp_path).read_text())["tokens"]
    assert saved["access_token"] and saved["account_id"] == "acct_b"


def test_browser_login_rejects_state_mismatch(tmp_path: Path) -> None:
    pasted = "http://localhost:1455/auth/callback?code=abc&state=not-the-real-state"
    with pytest.raises(ChatGptAuthError):
        browser_login(tmp_path, prompt=lambda _: pasted, echo=lambda _m: None)


# --- device-code flow ------------------------------------------------------


def test_device_login_polls_then_saves(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(chatgpt_login, "request_device_code",
                        lambda: {"device_auth_id": "d1", "user_code": "WXYZ-1234", "interval": 0})
    calls = {"n": 0}
    def fake_poll(did, uc):
        calls["n"] += 1
        return None if calls["n"] < 2 else {"authorization_code": "ac", "code_verifier": "cv"}
    monkeypatch.setattr(chatgpt_login, "poll_device", fake_poll)
    monkeypatch.setattr(chatgpt_login, "exchange_code",
                        lambda **kw: {"access_token": _access("acct_d"), "refresh_token": "r"})
    result = device_login(tmp_path, echo=lambda _m: None, sleep=lambda _s: None)
    assert result["account_id"] == "acct_d"
    assert calls["n"] == 2  # polled until approved
    assert json.loads(jigga_store(tmp_path).read_text())["tokens"]["account_id"] == "acct_d"


def test_device_login_times_out(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(chatgpt_login, "request_device_code",
                        lambda: {"device_auth_id": "d1", "user_code": "C", "interval": 1})
    monkeypatch.setattr(chatgpt_login, "poll_device", lambda did, uc: None)
    with pytest.raises(ChatGptAuthError):
        device_login(tmp_path, echo=lambda _m: None, sleep=lambda _s: None, max_seconds=2)


# --- store preference (JIGGA wins over codex) ------------------------------


def test_jigga_store_is_preferred_over_codex(tmp_path: Path, monkeypatch) -> None:
    # JIGGA's own store
    save_credentials(tmp_path, {"access_token": _access("acct_jigga"), "refresh_token": "r"})
    # a codex store that should be ignored when JIGGA's exists
    codex = tmp_path / "codex" / "auth.json"
    codex.parent.mkdir(parents=True)
    codex.write_text(json.dumps({"tokens": {"access_token": _access("acct_codex"), "refresh_token": "r"}}))
    monkeypatch.setattr("jigga.runtime.chatgpt_auth.codex_store", lambda: codex)

    creds = load_credentials(home=tmp_path)
    assert creds.account_id == "acct_jigga"
    assert login_state(tmp_path)["source"] == "jigga"


def test_falls_back_to_codex_when_no_jigga_store(tmp_path: Path, monkeypatch) -> None:
    codex = tmp_path / "codex" / "auth.json"
    codex.parent.mkdir(parents=True)
    codex.write_text(json.dumps({"tokens": {"access_token": _access("acct_codex"), "refresh_token": "r"}}))
    monkeypatch.setattr("jigga.runtime.chatgpt_auth.codex_store", lambda: codex)

    creds = load_credentials(home=tmp_path)
    assert creds.account_id == "acct_codex"
    assert login_state(tmp_path)["source"] == "codex"


# --- interactive onboarding (jigga model setup) ----------------------------


def _scripted(answers: list[str]):
    it = iter(answers)
    return lambda _p: next(it)


def test_model_setup_dry_run(tmp_path: Path) -> None:
    from jigga.cli import _model_setup
    from jigga.commands.init import init_runtime
    from jigga.core.io import read_yaml

    paths = init_runtime(tmp_path)
    _model_setup(paths, prompt=_scripted(["2"]), echo=lambda _m: None)
    assert read_yaml(paths.config)["models"]["defaults"]["provider"] == "dry_run"


def test_model_setup_chatgpt_skip_login(tmp_path: Path, monkeypatch) -> None:
    from jigga.cli import _model_setup
    from jigga.commands.init import init_runtime
    from jigga.core.io import read_yaml

    monkeypatch.setattr("jigga.runtime.chatgpt_auth.codex_store", lambda: tmp_path / "nope.json")
    paths = init_runtime(tmp_path)
    _model_setup(paths, prompt=_scripted(["1", "3"]), echo=lambda _m: None)  # chatgpt, then skip
    models = read_yaml(paths.config)["models"]
    assert models["defaults"]["provider"] == "chatgpt"
    assert models["providers"]["chatgpt"]["kind"] == "chatgpt_oauth"


def test_model_setup_detects_existing_login(tmp_path: Path, monkeypatch) -> None:
    from jigga.cli import _model_setup
    from jigga.commands.init import init_runtime

    monkeypatch.setattr("jigga.runtime.chatgpt_auth.codex_store", lambda: tmp_path / "nope.json")
    paths = init_runtime(tmp_path)
    save_credentials(tmp_path, {"access_token": _access("acct_x"), "refresh_token": "r"})
    calls = {"n": 0}
    def prompt(_p):
        calls["n"] += 1
        return "1"  # pick chatgpt
    _model_setup(paths, prompt=prompt, echo=lambda _m: None)
    assert calls["n"] == 1  # existing login detected → no auth-method prompt

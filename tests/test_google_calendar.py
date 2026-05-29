from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.runtime.google_calendar import (
    CALENDAR_READONLY_SCOPE,
    OAuthClientConfig,
    TokenSet,
    build_authorize_url,
    delete_tokens,
    generate_pkce_pair,
    get_valid_tokens,
    google_calendar_handler,
    load_client_config,
    load_tokens,
    normalize_event,
    refresh_access_token,
    store_client_config,
    store_tokens,
)


# --- OAuthClientConfig parsing ---------------------------------------------


def test_client_config_accepts_installed_shape() -> None:
    config = OAuthClientConfig.from_dict(
        {
            "installed": {
                "client_id": "abc.apps.googleusercontent.com",
                "client_secret": "shh",
                "redirect_uris": ["http://localhost"],
            }
        }
    )
    assert config.client_id == "abc.apps.googleusercontent.com"
    assert config.client_secret == "shh"


def test_client_config_accepts_web_shape() -> None:
    config = OAuthClientConfig.from_dict(
        {"web": {"client_id": "x", "client_secret": "y"}}
    )
    assert config.client_id == "x"


def test_client_config_rejects_missing_secret() -> None:
    with pytest.raises(ValueError, match="missing client_id or client_secret"):
        OAuthClientConfig.from_dict({"installed": {"client_id": "x"}})


# --- PKCE helpers ----------------------------------------------------------


def test_generate_pkce_pair_returns_distinct_high_entropy_pair() -> None:
    v1, c1 = generate_pkce_pair()
    v2, c2 = generate_pkce_pair()
    assert v1 != v2
    assert c1 != c2
    # Verifier must be url-safe and at least 43 chars per RFC 7636.
    assert len(v1) >= 43
    # Challenge is base64-url(sha256(verifier)) without padding.
    assert "=" not in c1


def test_build_authorize_url_includes_required_params() -> None:
    config = OAuthClientConfig(client_id="cid", client_secret="cs")
    url = build_authorize_url(config, port=12345, state="STATE", challenge="CHAL")
    assert "client_id=cid" in url
    assert "code_challenge=CHAL" in url
    assert "code_challenge_method=S256" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A12345" in url
    assert f"scope={CALENDAR_READONLY_SCOPE.replace(':','%3A').replace('/','%2F')}" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url


# --- Token persistence -----------------------------------------------------


def _future_iso(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _past_iso(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def test_store_and_load_tokens_round_trip(tmp_path: Path) -> None:
    tokens = TokenSet(
        access_token="access-1",
        refresh_token="refresh-1",
        expires_at=_future_iso(),
    )
    store_tokens(tmp_path, tokens)
    reloaded = load_tokens(tmp_path)
    assert reloaded is not None
    assert reloaded.access_token == "access-1"
    assert reloaded.refresh_token == "refresh-1"


def test_load_tokens_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_tokens(tmp_path) is None


def test_store_client_config_validates_and_copies(tmp_path: Path) -> None:
    source = tmp_path / "downloaded.json"
    source.write_text(
        json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}),
        encoding="utf-8",
    )
    target = store_client_config(tmp_path, source)
    assert target.exists()
    config = load_client_config(tmp_path)
    assert config is not None
    assert config.client_id == "x"


def test_store_client_config_rejects_bad_shape(tmp_path: Path) -> None:
    source = tmp_path / "bad.json"
    source.write_text(json.dumps({"random": "blob"}), encoding="utf-8")
    with pytest.raises(ValueError):
        store_client_config(tmp_path, source)


def test_token_expiry_detection() -> None:
    fresh = TokenSet(access_token="a", refresh_token="r", expires_at=_future_iso(3600))
    stale = TokenSet(access_token="a", refresh_token="r", expires_at=_past_iso())
    assert fresh.is_expired() is False
    assert stale.is_expired() is True


def test_delete_tokens_removes_file(tmp_path: Path) -> None:
    store_tokens(tmp_path, TokenSet(access_token="a", refresh_token="r", expires_at=_future_iso()))
    assert delete_tokens(tmp_path) is True
    assert delete_tokens(tmp_path) is False  # idempotent


# --- Refresh + get_valid_tokens --------------------------------------------


def _fake_urlopen_factory(body: dict) -> MagicMock:
    """urlopen() returns a context manager wrapping an object with .read()."""
    response = MagicMock()
    response.read.return_value = json.dumps(body).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value = response
    cm.__exit__.return_value = False
    return MagicMock(return_value=cm)


def test_refresh_access_token_keeps_refresh_when_absent_from_response(tmp_path: Path) -> None:
    config = OAuthClientConfig(client_id="cid", client_secret="cs")
    stored = TokenSet(access_token="old", refresh_token="r-1", expires_at=_past_iso())
    fake = _fake_urlopen_factory(
        {"access_token": "new", "expires_in": 1200, "token_type": "Bearer"}
    )
    with patch("jigga.runtime.google_calendar.urllib.request.urlopen", fake):
        refreshed = refresh_access_token(config, stored)
    assert refreshed.access_token == "new"
    assert refreshed.refresh_token == "r-1"  # carried over from caller


def test_get_valid_tokens_returns_existing_when_fresh(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    secrets = tmp_path / "secrets"
    store_tokens(secrets, TokenSet(access_token="a", refresh_token="r", expires_at=_future_iso()))
    fresh = get_valid_tokens(secrets)
    assert fresh is not None
    assert fresh.access_token == "a"


def test_get_valid_tokens_refreshes_when_expired(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    secrets = tmp_path / "secrets"
    client_json = tmp_path / "client.json"
    client_json.write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}),
        encoding="utf-8",
    )
    store_client_config(secrets, client_json)
    store_tokens(secrets, TokenSet(access_token="old", refresh_token="r", expires_at=_past_iso()))
    fake = _fake_urlopen_factory(
        {"access_token": "fresh", "expires_in": 3600, "refresh_token": "r"}
    )
    with patch("jigga.runtime.google_calendar.urllib.request.urlopen", fake):
        tokens = get_valid_tokens(secrets)
    assert tokens is not None
    assert tokens.access_token == "fresh"
    # And the refresh was persisted to disk.
    reloaded = load_tokens(secrets)
    assert reloaded is not None
    assert reloaded.access_token == "fresh"


def test_get_valid_tokens_returns_none_when_no_tokens(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    assert get_valid_tokens(tmp_path / "secrets") is None


# --- Event normalization ---------------------------------------------------


def test_normalize_event_handles_datetime_start() -> None:
    raw = {
        "id": "evt1",
        "summary": "Standup",
        "start": {"dateTime": "2026-06-01T09:30:00-04:00"},
        "location": "Zoom",
        "attendees": [{"email": "a@example.com", "responseStatus": "accepted"}],
        "hangoutLink": "https://meet.example.com/abc",
    }
    out = normalize_event(raw)
    assert out["title"] == "Standup"
    assert out["time"] == "2026-06-01T09:30:00-04:00"
    assert out["location"] == "Zoom"
    assert out["attendees"] == [{"email": "a@example.com", "response": "accepted"}]
    assert out["url"] == "https://meet.example.com/abc"


def test_normalize_event_handles_all_day_event() -> None:
    raw = {"id": "evt2", "start": {"date": "2026-06-01"}, "summary": "Holiday"}
    out = normalize_event(raw)
    assert out["time"] == "2026-06-01"
    assert out["title"] == "Holiday"


def test_normalize_event_uses_fallback_title_for_missing_summary() -> None:
    out = normalize_event({"id": "evt3", "start": {"dateTime": "2026-06-01T00:00:00Z"}})
    assert out["title"] == "(no title)"


# --- Handler dispatch ------------------------------------------------------


from dataclasses import dataclass


@dataclass
class _StubRuntime:
    home: Path
    agent: object = None


def _make_runtime(tmp_path: Path) -> _StubRuntime:
    init_runtime(tmp_path)
    return _StubRuntime(home=tmp_path)


def _step(action: str, input_dict: dict | None = None):
    from jigga.core.models import WorkflowStep
    return WorkflowStep(id="t", action=action, input=input_dict or {})


def test_handler_returns_not_connected_when_no_client_config(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    result = google_calendar_handler(
        _step("google_calendar.list_events"),
        _capability=None,
        resolved_input={},
        _memory_context={},
        runtime=runtime,
    )
    assert result["status"] == "google-calendar.not_connected"
    assert "capabilities install" in result["message"]


def test_handler_returns_not_connected_when_client_present_but_no_tokens(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    client_json = tmp_path / "client.json"
    client_json.write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}),
        encoding="utf-8",
    )
    store_client_config(tmp_path / "secrets", client_json)
    result = google_calendar_handler(
        _step("google_calendar.list_events"),
        _capability=None,
        resolved_input={},
        _memory_context={},
        runtime=runtime,
    )
    assert result["status"] == "google-calendar.not_connected"
    assert "calendar login" in result["message"]


def test_handler_list_events_returns_normalized_payload(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    secrets = tmp_path / "secrets"
    client_json = tmp_path / "client.json"
    client_json.write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}),
        encoding="utf-8",
    )
    store_client_config(secrets, client_json)
    store_tokens(secrets, TokenSet(access_token="a", refresh_token="r", expires_at=_future_iso()))
    fake_api = _fake_urlopen_factory(
        {
            "items": [
                {
                    "id": "evt1",
                    "summary": "Test event",
                    "start": {"dateTime": "2026-06-01T09:30:00Z"},
                }
            ],
            "nextPageToken": None,
        }
    )
    with patch("jigga.runtime.google_calendar.urllib.request.urlopen", fake_api):
        result = google_calendar_handler(
            _step("google_calendar.list_events"),
            _capability=None,
            resolved_input={"max_results": 5},
            _memory_context={},
            runtime=runtime,
        )
    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["events"][0]["title"] == "Test event"


def test_handler_get_event_requires_event_id(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    secrets = tmp_path / "secrets"
    client_json = tmp_path / "client.json"
    client_json.write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}),
        encoding="utf-8",
    )
    store_client_config(secrets, client_json)
    store_tokens(secrets, TokenSet(access_token="a", refresh_token="r", expires_at=_future_iso()))
    with pytest.raises(ValueError, match="event_id"):
        google_calendar_handler(
            _step("google_calendar.get_event"),
            _capability=None,
            resolved_input={},
            _memory_context={},
            runtime=runtime,
        )


# --- CLI integration -------------------------------------------------------


def test_cli_capabilities_list_available_shows_google_calendar(tmp_path: Path, capsys) -> None:
    assert main(["--home", str(tmp_path), "capabilities", "list-available"]) == 0
    output = capsys.readouterr().out
    assert "google-calendar" in output


def test_cli_calendar_status_reports_disconnected_state(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "calendar", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["client_config_present"] is False
    assert payload["tokens_present"] is False


def test_cli_calendar_login_errors_cleanly_without_client_config(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "calendar", "login"]) == 1
    assert "capabilities install google-calendar" in capsys.readouterr().err


def test_cli_calendar_logout_idempotent(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "calendar", "logout"]) == 0
    assert "No tokens to remove" in capsys.readouterr().out


def test_cli_init_no_prompt_skips_install_menu(tmp_path: Path, capsys) -> None:
    assert main(["--home", str(tmp_path), "init", "--no-prompt"]) == 0
    output = capsys.readouterr().out
    assert "Initialized JIGGA home" in output
    # The interactive menu prompt should NOT appear under --no-prompt
    assert "Install one now" not in output


# --- Install / uninstall command -------------------------------------------


def test_install_capability_copies_manifest_runs_setup_and_records_approval(tmp_path: Path) -> None:
    from jigga.commands.install import install_capability
    paths = init_runtime(tmp_path)

    inputs = iter(["/non/existent/first/try.json", str(tmp_path / "client.json"), "q"])  # final 'q' ignored
    client_json = tmp_path / "client.json"
    client_json.write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}),
        encoding="utf-8",
    )

    fake_runner = MagicMock(return_value=TokenSet(
        access_token="a", refresh_token="r", expires_at=_future_iso(),
    ))
    fake_list = _fake_urlopen_factory({"items": []})

    # Patch the oauth_runner used inside setup() and the API verification call.
    with patch(
        "jigga.optional_capabilities.google_calendar.run_oauth_flow", fake_runner
    ), patch(
        "jigga.runtime.google_calendar.urllib.request.urlopen", fake_list
    ):
        exit_code = install_capability(
            paths,
            name="google-calendar",
            input_fn=lambda _: next(inputs),
            print_fn=lambda *a, **k: None,
        )

    assert exit_code == 0
    # Manifest copied
    assert (paths.capabilities / "google-calendar" / "manifest.yaml").exists()
    # Approval recorded
    from jigga.runtime.capabilities import approvals_path
    payload = json.loads(approvals_path(paths.policies).read_text(encoding="utf-8"))
    assert "google-calendar" in payload["approvals"]
    # Client config in secrets dir
    assert (paths.secrets / "google_calendar_client.json").exists()


def test_install_unknown_capability_lists_available(tmp_path: Path) -> None:
    from jigga.commands.install import install_capability
    paths = init_runtime(tmp_path)
    lines: list[str] = []
    exit_code = install_capability(
        paths,
        name="nope",
        input_fn=lambda _: "",
        print_fn=lambda *a, **k: lines.append(" ".join(str(x) for x in a)),
    )
    assert exit_code == 1
    assert any("Available" in line for line in lines)
    assert any("google-calendar" in line for line in lines)


def test_uninstall_capability_removes_manifest_approval_and_secrets(tmp_path: Path) -> None:
    from jigga.commands.install import install_capability, uninstall_capability
    paths = init_runtime(tmp_path)

    client_json = tmp_path / "client.json"
    client_json.write_text(
        json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}),
        encoding="utf-8",
    )
    inputs = iter([str(client_json)])
    fake_runner = MagicMock(return_value=TokenSet(
        access_token="a", refresh_token="r", expires_at=_future_iso(),
    ))
    fake_list = _fake_urlopen_factory({"items": []})
    with patch(
        "jigga.optional_capabilities.google_calendar.run_oauth_flow", fake_runner
    ), patch(
        "jigga.runtime.google_calendar.urllib.request.urlopen", fake_list
    ):
        install_capability(
            paths, name="google-calendar",
            input_fn=lambda _: next(inputs), print_fn=lambda *a, **k: None,
        )

    # Stage some tokens to verify they're also removed.
    store_tokens(paths.secrets, TokenSet(access_token="a", refresh_token="r", expires_at=_future_iso()))

    exit_code = uninstall_capability(paths, name="google-calendar", print_fn=lambda *a, **k: None)
    assert exit_code == 0
    assert not (paths.capabilities / "google-calendar").exists()
    assert not (paths.secrets / "google_calendar_client.json").exists()
    assert not (paths.secrets / "google_calendar_tokens.json").exists()
    # Approval entry dropped
    from jigga.runtime.capabilities import approvals_path
    if approvals_path(paths.policies).exists():
        payload = json.loads(approvals_path(paths.policies).read_text(encoding="utf-8"))
        assert "google-calendar" not in (payload.get("approvals") or {})


def test_optional_registry_has_google_calendar_entry() -> None:
    from jigga.optional_capabilities import REGISTRY, list_available
    assert "google-calendar" in REGISTRY
    available = list_available()
    assert any(c.name == "google-calendar" for c in available)

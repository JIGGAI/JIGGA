from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.models import WorkflowStep
from jigga.runtime.gog import (
    GOGCLI_MARKERS,
    SUPPORTED_ACTIONS,
    gog_auth_status,
    gog_binary_status,
    gog_handler,
    keyring_password_path,
    load_keyring_password,
    run_gog_interactive,
    store_keyring_password,
)


@dataclass
class _StubRuntime:
    home: Path
    agent: object = None


def _step(action: str, input_dict: dict | None = None) -> WorkflowStep:
    return WorkflowStep(id="t", action=action, input=input_dict or {})


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


# --- keyring password storage ----------------------------------------------


def test_keyring_password_round_trip(tmp_path: Path) -> None:
    assert load_keyring_password(tmp_path) is None
    store_keyring_password(tmp_path, "hunter2")
    assert load_keyring_password(tmp_path) == "hunter2"


def test_keyring_password_path_shape(tmp_path: Path) -> None:
    assert keyring_password_path(tmp_path).name == "gog_keyring_password"


# --- binary detection / name-collision guard -------------------------------


def test_binary_status_reports_absent_when_not_on_path(monkeypatch) -> None:
    monkeypatch.setattr("jigga.runtime.gog.shutil.which", lambda _: None)
    status = gog_binary_status()
    assert status["available"] is False
    assert status["is_gogcli"] is False


def test_binary_status_accepts_real_gogcli_output(monkeypatch) -> None:
    monkeypatch.setattr("jigga.runtime.gog.shutil.which", lambda _: "/usr/local/bin/gog")
    monkeypatch.setattr(
        "jigga.runtime.gog.subprocess.run",
        lambda *a, **k: _completed(stdout="Available services: gmail, calendar, drive, sheets"),
    )
    status = gog_binary_status()
    assert status["available"] is True
    assert status["is_gogcli"] is True


def test_binary_status_rejects_name_collision(monkeypatch) -> None:
    # The unrelated node-based `gog` script runner echoes our argv:
    # `gog auth services` → "gog: auth not installed". No gogcli markers.
    monkeypatch.setattr("jigga.runtime.gog.shutil.which", lambda _: "/usr/bin/gog")
    monkeypatch.setattr(
        "jigga.runtime.gog.subprocess.run",
        lambda *a, **k: _completed(returncode=0, stderr="gog: auth not installed"),
    )
    status = gog_binary_status()
    assert status["available"] is True
    assert status["is_gogcli"] is False
    assert "does not look like gogcli" in status["reason"]


def test_binary_status_requires_two_markers(monkeypatch) -> None:
    # A single coincidental marker is not enough.
    monkeypatch.setattr("jigga.runtime.gog.shutil.which", lambda _: "/usr/bin/gog")
    monkeypatch.setattr(
        "jigga.runtime.gog.subprocess.run",
        lambda *a, **k: _completed(stdout="this mentions gmail once and nothing else relevant"),
    )
    assert gog_binary_status()["is_gogcli"] is False


def test_markers_exclude_argv_words() -> None:
    # Guard against regression: never match on words we pass as arguments.
    for forbidden in ("auth", "services", "doctor"):
        assert forbidden not in GOGCLI_MARKERS


# --- auth status -----------------------------------------------------------


def test_auth_status_short_circuits_when_not_gogcli(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "jigga.runtime.gog.gog_binary_status",
        lambda: {"available": True, "is_gogcli": False, "path": "/usr/bin/gog", "reason": "nope"},
    )
    status = gog_auth_status(tmp_path)
    assert status["connected"] is False


def test_auth_status_connected_when_doctor_succeeds(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "jigga.runtime.gog.gog_binary_status",
        lambda: {"available": True, "is_gogcli": True, "path": "/usr/local/bin/gog", "reason": None},
    )
    monkeypatch.setattr("jigga.runtime.gog.run_sandboxed", lambda spec, **k: _completed(returncode=0))
    status = gog_auth_status(tmp_path)
    assert status["connected"] is True


def test_auth_status_disconnected_when_doctor_fails(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "jigga.runtime.gog.gog_binary_status",
        lambda: {"available": True, "is_gogcli": True, "path": "/usr/local/bin/gog", "reason": None},
    )
    monkeypatch.setattr(
        "jigga.runtime.gog.run_sandboxed",
        lambda spec, **k: _completed(returncode=1, stderr="no account"),
    )
    status = gog_auth_status(tmp_path)
    assert status["connected"] is False


# --- keyring env injection -------------------------------------------------


def test_action_calls_inject_keyring_env(monkeypatch, tmp_path: Path) -> None:
    store_keyring_password(tmp_path / "secrets", "pw-123")
    captured = {}

    def fake_run_sandboxed(spec, **kwargs):
        captured["extra_env"] = spec.extra_env
        captured["args"] = spec.args
        return _completed(stdout=json.dumps({"threads": []}))

    monkeypatch.setattr(
        "jigga.runtime.gog.gog_binary_status",
        lambda: {"available": True, "is_gogcli": True, "path": "/x/gog", "reason": None},
    )
    monkeypatch.setattr("jigga.runtime.gog.run_sandboxed", fake_run_sandboxed)
    runtime = _StubRuntime(home=tmp_path)
    gog_handler(_step("gog.gmail_search", {"query": "is:unread"}), None, {"query": "is:unread"}, {}, runtime)

    assert captured["extra_env"]["GOG_KEYRING_BACKEND"] == "file"
    assert captured["extra_env"]["GOG_KEYRING_PASSWORD"] == "pw-123"
    # Global --json comes first, then the subcommand.
    assert captured["args"][0] == "--json"
    assert captured["args"][1:4] == ["gmail", "search", "is:unread"]


# --- action dispatch + argv mapping ----------------------------------------


def _handler_with_fake_gog(monkeypatch, tmp_path, *, stdout: str = "{}"):
    monkeypatch.setattr(
        "jigga.runtime.gog.gog_binary_status",
        lambda: {"available": True, "is_gogcli": True, "path": "/x/gog", "reason": None},
    )
    calls = {}

    def fake_run_sandboxed(spec, **kwargs):
        calls["args"] = spec.args
        return _completed(stdout=stdout)

    monkeypatch.setattr("jigga.runtime.gog.run_sandboxed", fake_run_sandboxed)
    store_keyring_password(tmp_path / "secrets", "pw")
    return calls


def test_gmail_search_maps_args(monkeypatch, tmp_path: Path) -> None:
    calls = _handler_with_fake_gog(monkeypatch, tmp_path, stdout=json.dumps({"threads": [{"id": "t1"}]}))
    runtime = _StubRuntime(home=tmp_path)
    result = gog_handler(
        _step("gog.gmail_search", {"query": "has:attachment", "max": 5}),
        None,
        {"query": "has:attachment", "max": 5},
        {},
        runtime,
    )
    assert result["status"] == "ok"
    assert result["data"] == {"threads": [{"id": "t1"}]}
    assert calls["args"] == ["--json", "gmail", "search", "has:attachment", "--max", "5"]


def test_gmail_get_requires_message_id(monkeypatch, tmp_path: Path) -> None:
    _handler_with_fake_gog(monkeypatch, tmp_path)
    runtime = _StubRuntime(home=tmp_path)
    with pytest.raises(ValueError, match="message_id"):
        gog_handler(_step("gog.gmail_get"), None, {}, {}, runtime)


def test_gmail_draft_maps_args(monkeypatch, tmp_path: Path) -> None:
    calls = _handler_with_fake_gog(monkeypatch, tmp_path)
    runtime = _StubRuntime(home=tmp_path)
    gog_handler(
        _step("gog.gmail_draft", {"to": "a@b.com", "subject": "Hi"}),
        None,
        {"to": "a@b.com", "subject": "Hi"},
        {},
        runtime,
    )
    assert calls["args"] == ["--json", "gmail", "drafts", "create", "--to", "a@b.com", "--subject", "Hi"]


def test_calendar_events_today_by_default(monkeypatch, tmp_path: Path) -> None:
    calls = _handler_with_fake_gog(monkeypatch, tmp_path, stdout=json.dumps({"events": []}))
    runtime = _StubRuntime(home=tmp_path)
    gog_handler(_step("gog.calendar_events"), None, {}, {}, runtime)
    assert calls["args"] == ["--json", "calendar", "events", "--today"]


def test_unknown_action_raises(monkeypatch, tmp_path: Path) -> None:
    _handler_with_fake_gog(monkeypatch, tmp_path)
    runtime = _StubRuntime(home=tmp_path)
    with pytest.raises(ValueError, match="Unknown gog action"):
        gog_handler(_step("gog.translate"), None, {}, {}, runtime)


# --- Drive -----------------------------------------------------------------


def test_drive_list_defaults_to_root(monkeypatch, tmp_path: Path) -> None:
    calls = _handler_with_fake_gog(monkeypatch, tmp_path, stdout=json.dumps({"tree": []}))
    runtime = _StubRuntime(home=tmp_path)
    gog_handler(_step("gog.drive_list"), None, {}, {}, runtime)
    assert calls["args"] == ["--json", "drive", "tree", "--parent", "root"]


def test_drive_list_with_folder_and_depth(monkeypatch, tmp_path: Path) -> None:
    calls = _handler_with_fake_gog(monkeypatch, tmp_path, stdout=json.dumps({"tree": []}))
    runtime = _StubRuntime(home=tmp_path)
    gog_handler(
        _step("gog.drive_list", {"folder_id": "abc", "depth": 2}),
        None,
        {"folder_id": "abc", "depth": 2},
        {},
        runtime,
    )
    assert calls["args"] == ["--json", "drive", "tree", "--parent", "abc", "--depth", "2"]


def test_drive_get_requires_file_id(monkeypatch, tmp_path: Path) -> None:
    _handler_with_fake_gog(monkeypatch, tmp_path)
    runtime = _StubRuntime(home=tmp_path)
    with pytest.raises(ValueError, match="file_id"):
        gog_handler(_step("gog.drive_get"), None, {}, {}, runtime)


def test_drive_share_refused_without_confirm(monkeypatch, tmp_path: Path) -> None:
    _handler_with_fake_gog(monkeypatch, tmp_path)
    runtime = _StubRuntime(home=tmp_path)
    result = gog_handler(
        _step("gog.drive_share", {"file_id": "f1", "email": "a@b.com"}),
        None,
        {"file_id": "f1", "email": "a@b.com"},
        {},
        runtime,
    )
    assert result["status"] == "gog.share_refused"


def test_drive_share_proceeds_with_confirm(monkeypatch, tmp_path: Path) -> None:
    calls = _handler_with_fake_gog(monkeypatch, tmp_path, stdout=json.dumps({"shared": True}))
    runtime = _StubRuntime(home=tmp_path)
    result = gog_handler(
        _step(
            "gog.drive_share",
            {"file_id": "f1", "email": "a@b.com", "notify": True, "confirm_share": True},
        ),
        None,
        {"file_id": "f1", "email": "a@b.com", "notify": True, "confirm_share": True},
        {},
        runtime,
    )
    assert result["status"] == "ok"
    assert calls["args"] == [
        "--json", "drive", "share", "f1", "--to", "user", "--email", "a@b.com", "--notify",
    ]


# --- Sheets ----------------------------------------------------------------


def test_sheets_get_maps_range(monkeypatch, tmp_path: Path) -> None:
    calls = _handler_with_fake_gog(monkeypatch, tmp_path, stdout=json.dumps({"values": []}))
    runtime = _StubRuntime(home=tmp_path)
    gog_handler(
        _step("gog.sheets_get", {"spreadsheet_id": "ss1", "range": "Sheet1!A1:D20"}),
        None,
        {"spreadsheet_id": "ss1", "range": "Sheet1!A1:D20"},
        {},
        runtime,
    )
    assert calls["args"] == ["--json", "sheets", "get", "ss1", "Sheet1!A1:D20"]


def test_sheets_get_requires_id_and_range(monkeypatch, tmp_path: Path) -> None:
    _handler_with_fake_gog(monkeypatch, tmp_path)
    runtime = _StubRuntime(home=tmp_path)
    with pytest.raises(ValueError, match="spreadsheet_id"):
        gog_handler(
            _step("gog.sheets_get", {"spreadsheet_id": "ss1"}),
            None,
            {"spreadsheet_id": "ss1"},
            {},
            runtime,
        )


def test_sheets_append_joins_values_with_pipe(monkeypatch, tmp_path: Path) -> None:
    calls = _handler_with_fake_gog(monkeypatch, tmp_path)
    runtime = _StubRuntime(home=tmp_path)
    gog_handler(
        _step("gog.sheets_append", {"spreadsheet_id": "ss1", "table": "Tasks", "values": ["Ship README", "done"]}),
        None,
        {"spreadsheet_id": "ss1", "table": "Tasks", "values": ["Ship README", "done"]},
        {},
        runtime,
    )
    assert calls["args"] == ["--json", "sheets", "table", "append", "ss1", "Tasks", "Ship README|done"]


def test_sheets_append_accepts_prejoined_row(monkeypatch, tmp_path: Path) -> None:
    calls = _handler_with_fake_gog(monkeypatch, tmp_path)
    runtime = _StubRuntime(home=tmp_path)
    gog_handler(
        _step("gog.sheets_append", {"spreadsheet_id": "ss1", "table": "Tasks", "row": "a|b"}),
        None,
        {"spreadsheet_id": "ss1", "table": "Tasks", "row": "a|b"},
        {},
        runtime,
    )
    assert calls["args"] == ["--json", "sheets", "table", "append", "ss1", "Tasks", "a|b"]


def test_sheets_append_requires_row_or_values(monkeypatch, tmp_path: Path) -> None:
    _handler_with_fake_gog(monkeypatch, tmp_path)
    runtime = _StubRuntime(home=tmp_path)
    with pytest.raises(ValueError, match="'row'.*'values'"):
        gog_handler(
            _step("gog.sheets_append", {"spreadsheet_id": "ss1", "table": "Tasks"}),
            None,
            {"spreadsheet_id": "ss1", "table": "Tasks"},
            {},
            runtime,
        )


# --- Docs ------------------------------------------------------------------


def test_docs_get_maps_args(monkeypatch, tmp_path: Path) -> None:
    calls = _handler_with_fake_gog(monkeypatch, tmp_path, stdout=json.dumps({"body": "..."}))
    runtime = _StubRuntime(home=tmp_path)
    gog_handler(_step("gog.docs_get", {"doc_id": "d1"}), None, {"doc_id": "d1"}, {}, runtime)
    assert calls["args"] == ["--json", "docs", "raw", "d1", "--pretty"]


def test_docs_write_maps_args(monkeypatch, tmp_path: Path) -> None:
    calls = _handler_with_fake_gog(monkeypatch, tmp_path)
    runtime = _StubRuntime(home=tmp_path)
    gog_handler(
        _step("gog.docs_write", {"doc_id": "d1", "text": "## Status"}),
        None,
        {"doc_id": "d1", "text": "## Status"},
        {},
        runtime,
    )
    assert calls["args"] == ["--json", "docs", "write", "d1", "--append", "--markdown", "--text", "## Status"]


def test_docs_write_requires_doc_id_and_text(monkeypatch, tmp_path: Path) -> None:
    _handler_with_fake_gog(monkeypatch, tmp_path)
    runtime = _StubRuntime(home=tmp_path)
    with pytest.raises(ValueError, match="doc_id"):
        gog_handler(_step("gog.docs_write", {"doc_id": "d1"}), None, {"doc_id": "d1"}, {}, runtime)


# --- send gating -----------------------------------------------------------


def test_send_refused_without_confirm(monkeypatch, tmp_path: Path) -> None:
    _handler_with_fake_gog(monkeypatch, tmp_path)
    runtime = _StubRuntime(home=tmp_path)
    result = gog_handler(
        _step("gog.gmail_send", {"to": "a@b.com"}),
        None,
        {"to": "a@b.com"},
        {},
        runtime,
    )
    assert result["status"] == "gog.send_refused"


def test_send_proceeds_with_confirm(monkeypatch, tmp_path: Path) -> None:
    calls = _handler_with_fake_gog(monkeypatch, tmp_path, stdout=json.dumps({"sent": True}))
    runtime = _StubRuntime(home=tmp_path)
    result = gog_handler(
        _step("gog.gmail_send", {"to": "a@b.com", "subject": "Hi", "confirm_send": True}),
        None,
        {"to": "a@b.com", "subject": "Hi", "confirm_send": True},
        {},
        runtime,
    )
    assert result["status"] == "ok"
    assert calls["args"] == ["--json", "gmail", "send", "--to", "a@b.com", "--subject", "Hi"]


# --- not-installed / not-gogcli degradation --------------------------------


def test_handler_returns_not_installed_when_absent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "jigga.runtime.gog.gog_binary_status",
        lambda: {"available": False, "is_gogcli": False, "path": None, "reason": "gog not on PATH"},
    )
    runtime = _StubRuntime(home=tmp_path)
    result = gog_handler(_step("gog.gmail_search"), None, {}, {}, runtime)
    assert result["status"] == "gog.not_installed"


def test_handler_returns_not_gogcli_on_collision(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "jigga.runtime.gog.gog_binary_status",
        lambda: {"available": True, "is_gogcli": False, "path": "/usr/bin/gog", "reason": "nope"},
    )
    runtime = _StubRuntime(home=tmp_path)
    result = gog_handler(_step("gog.gmail_search"), None, {}, {}, runtime)
    assert result["status"] == "gog.not_gogcli"


def test_run_gog_json_raises_on_nonzero(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "jigga.runtime.gog.gog_binary_status",
        lambda: {"available": True, "is_gogcli": True, "path": "/x/gog", "reason": None},
    )
    monkeypatch.setattr(
        "jigga.runtime.gog.run_sandboxed",
        lambda spec, **k: _completed(returncode=2, stderr="boom"),
    )
    store_keyring_password(tmp_path / "secrets", "pw")
    runtime = _StubRuntime(home=tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        gog_handler(_step("gog.gmail_search"), None, {}, {}, runtime)


# --- interactive runner ----------------------------------------------------


def test_run_gog_interactive_injects_keyring_env_and_returns_code(tmp_path: Path) -> None:
    store_keyring_password(tmp_path, "pw-xyz")
    captured = {}

    def fake_runner(argv, env=None, check=False):
        captured["argv"] = argv
        captured["env"] = env
        return _completed(returncode=0)

    code = run_gog_interactive(tmp_path, ["auth", "add", "me@gmail.com"], runner=fake_runner)
    assert code == 0
    assert captured["argv"] == ["gog", "auth", "add", "me@gmail.com"]
    assert captured["env"]["GOG_KEYRING_BACKEND"] == "file"
    assert captured["env"]["GOG_KEYRING_PASSWORD"] == "pw-xyz"


# --- registration + CLI ----------------------------------------------------


def test_gog_in_optional_registry() -> None:
    from jigga.optional_capabilities import REGISTRY
    assert "gog" in REGISTRY


def test_gog_handler_registered_in_dispatcher() -> None:
    from jigga.runtime.dispatcher import HANDLERS
    assert HANDLERS.get("runtime.gog") is not None


def test_supported_actions_constant_matches_manifest() -> None:
    import yaml
    manifest = Path(__file__).resolve().parents[1] / "jigga" / "optional_capabilities" / "gog" / "manifest.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert set(data["actions"]) == set(SUPPORTED_ACTIONS)


def test_cli_gog_status_smoke(tmp_path: Path, capsys, monkeypatch) -> None:
    init_runtime(tmp_path)
    monkeypatch.setattr(
        "jigga.runtime.gog.gog_binary_status",
        lambda: {"available": False, "is_gogcli": False, "path": None, "reason": "gog not on PATH"},
    )
    assert main(["--home", str(tmp_path), "gog", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["connected"] is False


def test_cli_gog_logout_idempotent(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "gog", "logout"]) == 0
    assert "No stored gog keyring password" in capsys.readouterr().out
    store_keyring_password(paths.secrets, "pw")
    assert main(["--home", str(tmp_path), "gog", "logout"]) == 0
    assert "Removed JIGGA's stored gog keyring password" in capsys.readouterr().out


# --- setup wizard ----------------------------------------------------------


def test_setup_wizard_happy_path(tmp_path: Path) -> None:
    from jigga.optional_capabilities.gog import setup
    paths = init_runtime(tmp_path)

    # gogcli present + gogcli-like
    with patch(
        "jigga.optional_capabilities.gog.gog_binary_status",
        return_value={"available": True, "is_gogcli": True, "path": "/x/gog", "reason": None},
    ), patch(
        "jigga.optional_capabilities.gog.gog_auth_status",
        return_value={"connected": True},
    ):
        interactive_calls = []

        def fake_interactive_runner(argv, env=None, check=False):
            interactive_calls.append(argv)
            return _completed(returncode=0)

        inputs = iter([
            "/path/to/client.json",   # client JSON path
            "keyring-pw",              # keyring password
            "me@gmail.com",            # email
        ])
        exit_code = setup(
            paths,
            input_fn=lambda _: next(inputs),
            print_fn=lambda *a, **k: None,
            interactive_runner=fake_interactive_runner,
        )
    assert exit_code == 0
    # keyring password persisted
    assert load_keyring_password(paths.secrets) == "keyring-pw"
    # both interactive gog calls happened: credentials then auth add
    assert any("credentials" in argv for argv in interactive_calls)
    assert any("add" in argv for argv in interactive_calls)


def test_setup_wizard_aborts_when_gogcli_missing(tmp_path: Path) -> None:
    from jigga.optional_capabilities.gog import setup
    paths = init_runtime(tmp_path)
    with patch(
        "jigga.optional_capabilities.gog.gog_binary_status",
        return_value={"available": False, "is_gogcli": False, "path": None, "reason": "x"},
    ):
        exit_code = setup(paths, input_fn=lambda _: "", print_fn=lambda *a, **k: None)
    assert exit_code == 1

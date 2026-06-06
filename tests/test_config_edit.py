"""`jigga config get|set|unset` — the dotted-key config surface (CLI-as-API
for the UI settings page; humans flip keys without an editor)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml
from jigga.runtime.config_edit import coerce_value, get_path, set_path, unset_path


# --- module -------------------------------------------------------------------


def test_coerce_json_first_string_fallback() -> None:
    assert coerce_value("true") is True
    assert coerce_value("42") == 42
    assert coerce_value("1.5") == 1.5
    assert coerce_value('["a", "b"]') == ["a", "b"]
    assert coerce_value('{"x": 1}') == {"x": 1}
    assert coerce_value("telegram") == "telegram"          # bare string needs no quoting
    assert coerce_value("7:30am") == "7:30am"


def test_get_set_unset_roundtrip() -> None:
    config: dict = {}
    assert get_path(config, "channels.default") is None
    assert set_path(config, "channels.default", "telegram") is None      # old value
    assert config == {"channels": {"default": "telegram"}}
    assert get_path(config, "channels.default") == "telegram"
    assert set_path(config, "channels.default", "slack") == "telegram"   # returns old
    assert unset_path(config, "channels.default") == "slack"
    assert get_path(config, "channels.default") is None


def test_set_refuses_to_descend_through_scalar() -> None:
    config = {"supervisor": {"interval_seconds": 60}}
    with pytest.raises(ValueError, match="not a map"):
        set_path(config, "supervisor.interval_seconds.nested", 1)
    assert config["supervisor"]["interval_seconds"] == 60               # untouched
    with pytest.raises(ValueError, match="Invalid config key"):
        set_path(config, "a..b", 1)


def test_unset_missing_is_safe() -> None:
    config = {"a": {"b": 1}}
    assert unset_path(config, "a.nope") is None
    assert unset_path(config, "nope.deep.path") is None
    assert config == {"a": {"b": 1}}


# --- CLI ----------------------------------------------------------------------


def test_cli_set_get_unset(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "config", "set", "channels.default", "telegram"]) == 0
    assert "→ 'telegram'" in capsys.readouterr().out
    assert read_yaml(tmp_path / "config.yaml")["channels"]["default"] == "telegram"

    assert main(["--home", str(tmp_path), "config", "get", "channels.default"]) == 0
    assert capsys.readouterr().out.strip() == "telegram"

    assert main(["--home", str(tmp_path), "config", "get", "channels.default", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == "telegram"

    assert main(["--home", str(tmp_path), "config", "unset", "channels.default"]) == 0
    capsys.readouterr()
    assert "default" not in read_yaml(tmp_path / "config.yaml").get("channels", {})


def test_cli_set_coerces_types_and_audits(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "config", "set",
                 "supervisor.max_wakes_per_agent_per_hour", "24"]) == 0
    capsys.readouterr()
    assert read_yaml(paths.config)["supervisor"]["max_wakes_per_agent_per_hour"] == 24

    events = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines()]
    changed = [e for e in events if e["type"] == "config.changed"]
    assert changed and changed[-1]["details"]["new"] == 24
    assert changed[-1]["details"]["old"] == 12                          # init default


def test_cli_get_whole_config_and_scalar_guard(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "config", "get", "--json"]) == 0
    whole = json.loads(capsys.readouterr().out)
    assert whole["version"] == 1 and "supervisor" in whole

    rc = main(["--home", str(tmp_path), "config", "set",
               "supervisor.interval_seconds.oops", "1"])
    assert rc == 1                                                       # refused, non-zero
    assert "not a map" in capsys.readouterr().out


# --- entity get/set (teams + agents) — the jiggaview editor surface ------------


def test_team_get_set_roundtrip(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path, examples=True)
    assert main(["--home", str(tmp_path), "team", "get", "marketing_team", "purpose"]) == 0
    assert "launch copy" in capsys.readouterr().out

    assert main(["--home", str(tmp_path), "team", "set", "marketing_team",
                 "purpose", "Ship the aurora launch."]) == 0
    capsys.readouterr()
    assert read_yaml(tmp_path / "teams" / "marketing_team.yaml")["purpose"] == "Ship the aurora launch."

    assert main(["--home", str(tmp_path), "team", "get", "marketing_team", "--json"]) == 0
    whole = json.loads(capsys.readouterr().out)
    assert whole["purpose"] == "Ship the aurora launch."

    events = [json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    changed = [e for e in events if e["type"] == "team.changed"]
    assert changed and changed[-1]["details"]["key"] == "purpose"


def test_agent_set_validates_and_rolls_back(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path, examples=True)
    before = (tmp_path / "agents" / "copywriter.yaml").read_text(encoding="utf-8")

    rc = main(["--home", str(tmp_path), "agents", "set", "copywriter",
               "permission_mode", "bogus_mode"])
    assert rc == 1
    assert "rolled back" in capsys.readouterr().out
    assert (tmp_path / "agents" / "copywriter.yaml").read_text(encoding="utf-8") == before

    assert main(["--home", str(tmp_path), "agents", "set", "copywriter",
                 "permission_mode", "autonomous"]) == 0
    capsys.readouterr()
    assert read_yaml(tmp_path / "agents" / "copywriter.yaml")["permission_mode"] == "autonomous"


def test_entity_get_missing_is_clean_error(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "team", "get", "nope"]) == 1
    assert "No such entity" in capsys.readouterr().out

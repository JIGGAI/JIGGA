"""`jigga update` (#88): plan → review → confirm reconciliation of a runtime
with the current code — recipe artifacts (three-way via #91 install records),
additive config migrations, service refresh."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.recipes import find_recipe, load_recipe, scaffold_agent
from jigga.runtime.update import apply_update, plan_update


@pytest.fixture(autouse=True)
def _no_real_service(monkeypatch):
    """Tests never touch launchd/systemd: status reports not-installed by
    default; install_service records instead of executing. Tests that assert
    service behavior override these."""
    installs: list = []
    monkeypatch.setattr("jigga.runtime.update.status_service",
                        lambda paths, **k: {"backend": "systemd", "installed": False})
    monkeypatch.setattr(
        "jigga.runtime.update.install_service",
        lambda paths, **kw: installs.append(kw) or {"backend": "systemd", "started": True})
    return installs


def _paths(tmp_path):
    return init_runtime(tmp_path)


def _scaffold_researcher(paths):
    recipe = load_recipe(find_recipe(paths.home, "researcher"))
    scaffold_agent(paths.home, recipe, agents_dir=paths.agents)
    return paths.agents / "researcher.yaml"


def _retarget_record_to_local_recipe(paths, recipe_text: str) -> Path:
    """Point the researcher's install record at a local (mutable) copy of the
    recipe, so tests can simulate 'the shipped recipe changed'."""
    local = paths.home / "recipes" / "researcher.md"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(recipe_text, encoding="utf-8")
    record_path = paths.home / "state" / "recipes" / "researcher.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["source"] = str(local)
    record_path.write_text(json.dumps(record), encoding="utf-8")
    return local


def test_clean_runtime_plans_nothing(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _scaffold_researcher(paths)
    plan = plan_update(paths)
    assert plan["actions"] == [] and plan["notices"] == []


def test_pristine_artifact_updates_when_shipped_recipe_changes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _scaffold_researcher(paths)
    original = find_recipe(paths.home, "researcher").read_text(encoding="utf-8")
    _retarget_record_to_local_recipe(
        paths, original.replace("Gathers and summarizes information",
                                "Hunts down and verifies information"))

    plan = plan_update(paths)
    updates = [a for a in plan["actions"] if a["kind"] == "artifact.update"]
    assert len(updates) == 1 and updates[0]["detail"]["path"] == "agents/researcher.yaml"

    results = apply_update(paths, plan)
    assert results["errors"] == []
    assert "Hunts down" in (paths.agents / "researcher.yaml").read_text(encoding="utf-8")
    # the record hash refreshed → a second update plans nothing (idempotent)...
    assert plan_update(paths)["actions"] == []
    # ...and the updated file reads as PRISTINE again, not as a local edit
    from jigga.runtime.recipes import installed_recipes
    record = next(r for r in installed_recipes(paths.home) if r["scaffold_id"] == "researcher")
    assert record["modified"] == []


def test_locally_edited_artifact_is_left_alone_with_notice(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_yaml = _scaffold_researcher(paths)
    agent_yaml.write_text(agent_yaml.read_text(encoding="utf-8") + "# my custom note\n",
                          encoding="utf-8")
    original = find_recipe(paths.home, "researcher").read_text(encoding="utf-8")
    _retarget_record_to_local_recipe(paths, original.replace("Gathers", "Hunts"))

    plan = plan_update(paths)
    assert not any(a["kind"].startswith("artifact") for a in plan["actions"])
    assert any("local edits" in n for n in plan["notices"])
    apply_update(paths, plan)
    assert "# my custom note" in agent_yaml.read_text(encoding="utf-8")   # untouched


def test_missing_artifact_is_recreated(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_yaml = _scaffold_researcher(paths)
    agent_yaml.unlink()
    plan = plan_update(paths)
    recreates = [a for a in plan["actions"] if a["kind"] == "artifact.recreate"]
    assert len(recreates) == 1
    apply_update(paths, plan)
    assert agent_yaml.exists()


def test_config_migration_adds_default_channel(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": True, "allowed_chat_ids": ["1"]}}  # pre-#87 shape
    write_yaml(paths.config, config)

    plan = plan_update(paths)
    migrations = [a for a in plan["actions"] if a["kind"] == "config.migrate"]
    assert len(migrations) == 1 and "channels.default" in migrations[0]["description"]
    apply_update(paths, plan)
    assert read_yaml(paths.config)["channels"]["default"] == "telegram"
    assert plan_update(paths)["actions"] == []                              # idempotent


def test_service_refresh_planned_and_preserves_interval(tmp_path: Path, monkeypatch,
                                                        _no_real_service) -> None:
    paths = _paths(tmp_path)
    unit = tmp_path / "jigga-supervisor.service"
    unit.write_text("ExecStart=/x/python -m jigga supervisor start --interval-seconds 90",
                    encoding="utf-8")
    monkeypatch.setattr("jigga.runtime.update.status_service",
                        lambda p, **k: {"backend": "systemd", "installed": True,
                                        "unit_path": str(unit)})

    plan = plan_update(paths)
    refresh = [a for a in plan["actions"] if a["kind"] == "service.refresh"]
    assert len(refresh) == 1 and refresh[0]["detail"]["interval_seconds"] == 90.0
    apply_update(paths, plan)
    assert _no_real_service and _no_real_service[0]["interval_seconds"] == 90.0


# --- CLI: prompt-to-apply ------------------------------------------------------


def test_cli_interactive_prompts_and_applies_on_yes(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _paths(tmp_path)
    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": True}}
    write_yaml(paths.config, config)
    monkeypatch.setattr("jigga.cli._confirm", lambda *a, **k: True)
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": staticmethod(lambda: True)})())

    assert main(["--home", str(tmp_path), "update"]) == 0
    out = capsys.readouterr().out
    assert "Planned changes" in out and "✓ Applied 1" in out
    assert read_yaml(paths.config)["channels"]["default"] == "telegram"


def test_cli_interactive_declined_applies_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _paths(tmp_path)
    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": True}}
    write_yaml(paths.config, config)
    monkeypatch.setattr("jigga.cli._confirm", lambda *a, **k: False)
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": staticmethod(lambda: True)})())

    assert main(["--home", str(tmp_path), "update"]) == 0
    assert "Not applied." in capsys.readouterr().out
    assert "default" not in read_yaml(paths.config)["channels"]


def test_cli_apply_flag_skips_prompt(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _paths(tmp_path)
    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": True}}
    write_yaml(paths.config, config)
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not prompt with --apply"))  # noqa: E731
    monkeypatch.setattr("jigga.cli._confirm", boom)

    assert main(["--home", str(tmp_path), "update", "--apply"]) == 0
    assert read_yaml(paths.config)["channels"]["default"] == "telegram"


def test_cli_non_interactive_without_apply_is_safe(tmp_path: Path, monkeypatch, capsys) -> None:
    """Piped/scripted without --apply: show the plan, apply nothing."""
    paths = _paths(tmp_path)
    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": True}}
    write_yaml(paths.config, config)
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": staticmethod(lambda: False)})())

    assert main(["--home", str(tmp_path), "update"]) == 0
    assert "jigga update --apply" in capsys.readouterr().out
    assert "default" not in read_yaml(paths.config)["channels"]


def test_cli_up_to_date_and_dry_run(tmp_path: Path, capsys) -> None:
    paths = _paths(tmp_path)
    assert main(["--home", str(tmp_path), "update"]) == 0
    assert "up to date" in capsys.readouterr().out

    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": True}}
    write_yaml(paths.config, config)
    assert main(["--home", str(tmp_path), "update", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "default" not in read_yaml(paths.config)["channels"]


# --- edited-divergent files: per-item picker + backups ---------------------------


def _make_edited(paths) -> Path:
    """Scaffold researcher, edit it locally, point its record at a changed
    recipe → planner reports it as edited-divergent."""
    agent_yaml = _scaffold_researcher(paths)
    agent_yaml.write_text(agent_yaml.read_text(encoding="utf-8") + "# my custom note\n",
                          encoding="utf-8")
    original = find_recipe(paths.home, "researcher").read_text(encoding="utf-8")
    _retarget_record_to_local_recipe(paths, original.replace("Gathers", "Hunts"))
    return agent_yaml


def test_plan_returns_structured_edited_entries(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _make_edited(paths)
    plan = plan_update(paths)
    assert len(plan["edited"]) == 1
    entry = plan["edited"][0]
    assert entry["path"] == "agents/researcher.yaml"
    assert entry["recipe"] == "researcher"
    assert "Hunts" in entry["content"]


def test_overwrite_edited_backs_up_replaces_and_re_pristines(tmp_path: Path) -> None:
    from jigga.runtime.recipes import installed_recipes
    from jigga.runtime.update import overwrite_edited

    paths = _paths(tmp_path)
    agent_yaml = _make_edited(paths)
    plan = plan_update(paths)

    results = overwrite_edited(paths, plan, ["agents/researcher.yaml"])
    assert results["errors"] == [] and results["replaced"] == ["agents/researcher.yaml"]
    # replaced with the shipped version...
    text = agent_yaml.read_text(encoding="utf-8")
    assert "Hunts" in text and "# my custom note" not in text
    # ...the edit is preserved in a backup...
    assert len(results["backups"]) == 1
    backup = Path(results["backups"][0])
    assert backup.is_relative_to(paths.home / "state" / "backups")
    assert "# my custom note" in backup.read_text(encoding="utf-8")
    # ...and the file reads pristine again (hash refreshed)
    record = next(r for r in installed_recipes(paths.home) if r["scaffold_id"] == "researcher")
    assert record["modified"] == []
    assert plan_update(paths)["edited"] == []


def test_overwrite_edited_never_touches_unselected(tmp_path: Path) -> None:
    from jigga.runtime.update import overwrite_edited

    paths = _paths(tmp_path)
    agent_yaml = _make_edited(paths)
    plan = plan_update(paths)
    results = overwrite_edited(paths, plan, [])                     # nothing selected
    assert results["replaced"] == [] and results["backups"] == []
    assert "# my custom note" in agent_yaml.read_text(encoding="utf-8")


def test_cli_picker_replaces_selected_and_prints_footer(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _paths(tmp_path)
    agent_yaml = _make_edited(paths)
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr("jigga.cli.supports_picker", lambda *a, **k: True)
    monkeypatch.setattr("jigga.cli.multi_select", lambda title, options, **k: [0])
    monkeypatch.setattr("jigga.cli._confirm", lambda *a, **k: True)

    assert main(["--home", str(tmp_path), "update"]) == 0
    out = capsys.readouterr().out
    # selection happens during review, BEFORE the apply confirm; mutation after
    assert out.index("replace agents/researcher.yaml") < out.index("✓ replaced agents/researcher.yaml")
    assert "backup:" in out
    assert "# my custom note" not in agent_yaml.read_text(encoding="utf-8")


def test_cli_picker_declined_keeps_files_and_prints_example(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _paths(tmp_path)
    agent_yaml = _make_edited(paths)
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr("jigga.cli.supports_picker", lambda *a, **k: True)
    monkeypatch.setattr("jigga.cli.multi_select", lambda title, options, **k: [])
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no confirm when nothing to apply"))  # noqa: E731
    monkeypatch.setattr("jigga.cli._confirm", boom)

    assert main(["--home", str(tmp_path), "update"]) == 0
    out = capsys.readouterr().out
    assert "Nothing to apply." in out
    assert "jigga recipes scaffold researcher --overwrite" in out   # the example command
    assert "# my custom note" in agent_yaml.read_text(encoding="utf-8")


def test_cli_non_interactive_edited_prints_example_only(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _paths(tmp_path)
    agent_yaml = _make_edited(paths)
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": staticmethod(lambda: False)})())

    assert main(["--home", str(tmp_path), "update"]) == 0
    out = capsys.readouterr().out
    assert "jigga recipes scaffold researcher --overwrite" in out
    assert "# my custom note" in agent_yaml.read_text(encoding="utf-8")


def test_cli_declining_confirm_rolls_back_nothing_including_selection(tmp_path: Path, monkeypatch, capsys) -> None:
    """The single confirm covers replacements too: declining after selecting
    must leave the edited file untouched (nothing mutates before consent)."""
    paths = _paths(tmp_path)
    agent_yaml = _make_edited(paths)
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr("jigga.cli.supports_picker", lambda *a, **k: True)
    monkeypatch.setattr("jigga.cli.multi_select", lambda title, options, **k: [0])
    monkeypatch.setattr("jigga.cli._confirm", lambda *a, **k: False)

    assert main(["--home", str(tmp_path), "update"]) == 0
    assert "Not applied." in capsys.readouterr().out
    assert "# my custom note" in agent_yaml.read_text(encoding="utf-8")
    assert not (paths.home / "state" / "backups").exists()


def test_cli_pristine_updates_are_picker_options_preselected(tmp_path: Path, monkeypatch, capsys) -> None:
    """RJ: any recipe change is a selection option — pristine updates arrive
    PRE-selected in the picker (safe default), then the apply confirm runs."""
    paths = _paths(tmp_path)
    _scaffold_researcher(paths)
    original = find_recipe(paths.home, "researcher").read_text(encoding="utf-8")
    _retarget_record_to_local_recipe(paths, original.replace("Gathers", "Hunts"))  # pristine file, recipe changed

    captured: dict = {}

    def fake_picker(title, options, **k):
        captured["preselected"] = [o.selected for o in options]
        captured["labels"] = [o.label for o in options]
        return [i for i, o in enumerate(options) if o.selected]   # accept defaults

    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr("jigga.cli.supports_picker", lambda *a, **k: True)
    monkeypatch.setattr("jigga.cli.multi_select", fake_picker)
    monkeypatch.setattr("jigga.cli._confirm", lambda *a, **k: True)

    assert main(["--home", str(tmp_path), "update"]) == 0
    out = capsys.readouterr().out
    assert captured["preselected"] == [True]                      # pristine = pre-selected
    assert captured["labels"] == ["agents/researcher.yaml"]
    # picker echo precedes the apply; the artifact is NOT in the flat plan list
    assert "Planned changes" not in out.split("update agents/researcher.yaml")[0]
    assert "Hunts" in (paths.agents / "researcher.yaml").read_text(encoding="utf-8")


def test_cli_deselecting_pristine_update_keeps_current_file(tmp_path: Path, monkeypatch, capsys) -> None:
    paths = _paths(tmp_path)
    _scaffold_researcher(paths)
    original = find_recipe(paths.home, "researcher").read_text(encoding="utf-8")
    _retarget_record_to_local_recipe(paths, original.replace("Gathers", "Hunts"))
    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": True}}          # one non-recipe action remains
    write_yaml(paths.config, config)

    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": staticmethod(lambda: True)})())
    monkeypatch.setattr("jigga.cli.supports_picker", lambda *a, **k: True)
    monkeypatch.setattr("jigga.cli.multi_select", lambda title, options, **k: [])  # deselect everything
    monkeypatch.setattr("jigga.cli._confirm", lambda *a, **k: True)

    assert main(["--home", str(tmp_path), "update"]) == 0
    assert "Hunts" not in (paths.agents / "researcher.yaml").read_text(encoding="utf-8")  # kept
    assert read_yaml(paths.config)["channels"]["default"] == "telegram"                   # rest applied


def test_cli_apply_flag_still_applies_pristine_without_picker(tmp_path: Path, monkeypatch) -> None:
    paths = _paths(tmp_path)
    _scaffold_researcher(paths)
    original = find_recipe(paths.home, "researcher").read_text(encoding="utf-8")
    _retarget_record_to_local_recipe(paths, original.replace("Gathers", "Hunts"))
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no picker with --apply"))  # noqa: E731
    monkeypatch.setattr("jigga.cli.multi_select", boom)

    assert main(["--home", str(tmp_path), "update", "--apply"]) == 0
    assert "Hunts" in (paths.agents / "researcher.yaml").read_text(encoding="utf-8")

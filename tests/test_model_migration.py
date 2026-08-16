"""Assertion 14 — config references outlive the names they point at.

An upgrade on the prior-gen stack merged the standalone `openai-codex` provider
into `openai`, and every legacy `openai-codex/*` reference was rejected:
`run error: Unknown model: openai-codex/gpt-5.5`. The official fix rewrote
model refs across defaults, agents **and stale sessions** — the last of which
is the part that gets forgotten, leaving config that looks migrated while old
runs keep failing.

Two kinds of staleness, only one of which needs a rename table:

- **renamed** — driven by `MODEL_RENAMES`, deliberately empty today because
  JIGGA has renamed nothing yet. The point of the assertion is to have the
  path before the first rename. The tests below inject renames rather than
  asserting invented ones into existence.
- **dangling** — a profile or provider that simply isn't configured. Live right
  now, and the more dangerous of the two because it fails *silently*:
  `call_model` falls back to the default profile, so an agent you believe is
  pinned to a specific model has been quietly running on another one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import read_json, read_yaml, write_json, write_yaml
from jigga.runtime import doctor, model_migration
from jigga.runtime.model_migration import migrate_model_refs, stale_model_refs


@pytest.fixture
def renames(monkeypatch):
    """Inject a rename table. The shipped one is empty on purpose."""
    monkeypatch.setattr(model_migration, "MODEL_RENAMES", {"openai-codex": "openai"})


def _configure(home: Path, **models) -> None:
    config = home / "config.yaml"
    data = read_yaml(config)
    data["models"] = models
    write_yaml(config, data)


def _agent(paths, agent_id: str, model: str | None) -> None:
    body = {"id": agent_id, "name": agent_id, "role": "r", "tools": []}
    if model is not None:
        body["model"] = model
    write_yaml(paths.agents / f"{agent_id}.yaml", body)


# --- dangling: live today, silent today -------------------------------------


def test_an_agent_pinned_to_a_missing_profile_is_reported(tmp_path: Path) -> None:
    """This is the silent one. `call_model` falls back to the default profile,
    so the agent runs on a model nobody chose for it and nothing says so."""
    paths = init_runtime(tmp_path)
    _configure(tmp_path, providers={"chatgpt": {"kind": "chatgpt_oauth", "default_model": "gpt-5"}},
               profiles={"default": {"primary": "chatgpt"}})
    _agent(paths, "thrifty", "profile:cheap")

    rows = stale_model_refs(paths)

    assert [(r["problem"], r["ref"]) for r in rows] == [("dangling", "profile:cheap")]
    assert "thrifty" in rows[0]["where"]


def test_an_agent_on_a_configured_profile_is_fine(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _configure(tmp_path, providers={"chatgpt": {"kind": "chatgpt_oauth", "default_model": "gpt-5"}},
               profiles={"default": {"primary": "chatgpt"}, "cheap": {"primary": "chatgpt"}})
    _agent(paths, "thrifty", "profile:cheap")
    assert stale_model_refs(paths) == []


def test_a_default_provider_that_is_not_configured_is_reported(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _configure(tmp_path, defaults={"provider": "ghost"},
               providers={"chatgpt": {"kind": "chatgpt_oauth", "default_model": "gpt-5"}},
               profiles={"default": {"primary": "chatgpt"}})

    rows = stale_model_refs(paths)

    assert any(r["problem"] == "dangling" and r["ref"] == "ghost" for r in rows)


def test_a_freshly_initialised_runtime_is_clean(tmp_path: Path) -> None:
    """The check must be quiet on a default install or it is noise."""
    paths = init_runtime(tmp_path)
    assert stale_model_refs(paths) == []
    assert doctor._check_model_refs(paths).status == doctor.OK


def test_dangling_refs_are_never_auto_fixed(tmp_path: Path) -> None:
    """Guessing which model the user meant is not a safe, non-interactive fix."""
    paths = init_runtime(tmp_path)
    _configure(tmp_path, providers={"chatgpt": {"kind": "chatgpt_oauth", "default_model": "gpt-5"}},
               profiles={"default": {"primary": "chatgpt"}})
    _agent(paths, "thrifty", "profile:cheap")

    assert migrate_model_refs(paths, apply=True)["changed"] == []
    assert read_yaml(paths.agents / "thrifty.yaml")["model"] == "profile:cheap"


# --- renamed: rewritable ----------------------------------------------------


def test_a_renamed_provider_is_detected(tmp_path: Path, renames) -> None:
    paths = init_runtime(tmp_path)
    _configure(tmp_path, defaults={"provider": "openai-codex"},
               providers={"openai-codex": {"kind": "openai_compatible", "default_model": "gpt-5.5"}},
               profiles={"default": {"primary": "openai-codex"}})

    rows = [r for r in stale_model_refs(paths) if r["problem"] == "renamed"]

    assert rows and all(r["suggestion"] == "openai" for r in rows)


def test_a_provider_qualified_model_keeps_its_model_half(tmp_path: Path, renames) -> None:
    """`openai-codex/gpt-5.5` → `openai/gpt-5.5`, not `openai`."""
    paths = init_runtime(tmp_path)
    _configure(tmp_path, providers={"openai": {"kind": "openai_compatible", "default_model": "gpt-5.5"}},
               profiles={"default": {"primary": "openai"}})
    _agent(paths, "writer", "openai-codex/gpt-5.5")

    rows = [r for r in stale_model_refs(paths) if r["problem"] == "renamed"]

    assert rows[0]["suggestion"] == "openai/gpt-5.5"


def test_the_migration_rewrites_config_and_agents(tmp_path: Path, renames) -> None:
    paths = init_runtime(tmp_path)
    _configure(tmp_path, defaults={"provider": "openai-codex"},
               providers={"openai": {"kind": "openai_compatible", "default_model": "gpt-5.5"}},
               profiles={"default": {"primary": "openai-codex"}})
    _agent(paths, "writer", "openai-codex/gpt-5.5")

    result = migrate_model_refs(paths, apply=True)

    assert result["applied"] is True and result["changed"]
    config = read_yaml(tmp_path / "config.yaml")
    assert config["models"]["defaults"]["provider"] == "openai"
    assert config["models"]["profiles"]["default"]["primary"] == "openai"
    assert read_yaml(paths.agents / "writer.yaml")["model"] == "openai/gpt-5.5"


def test_the_migration_also_rewrites_stale_session_and_run_state(tmp_path: Path, renames) -> None:
    """The half everyone forgets: config looks migrated while old records keep
    failing against a name that no longer exists."""
    paths = init_runtime(tmp_path)
    _configure(tmp_path, providers={"openai": {"kind": "openai_compatible", "default_model": "gpt-5.5"}},
               profiles={"default": {"primary": "openai"}})
    session = paths.sessions / "sess_1.json"
    write_json(session, {"id": "sess_1", "provider": "openai-codex", "model": "openai-codex/gpt-5.5"})
    run = paths.runs / "workflows" / "wf" / "run_1" / "run.json"
    write_json(run, {"id": "run_1", "nodes": {"n1": {"model": "openai-codex/gpt-5.5"}}})

    migrate_model_refs(paths, apply=True)

    assert read_json(session)["provider"] == "openai"
    assert read_json(session)["model"] == "openai/gpt-5.5"
    assert read_json(run)["nodes"]["n1"]["model"] == "openai/gpt-5.5"


def test_a_dry_run_changes_nothing(tmp_path: Path, renames) -> None:
    paths = init_runtime(tmp_path)
    _configure(tmp_path, defaults={"provider": "openai-codex"},
               providers={"openai": {"kind": "openai_compatible", "default_model": "gpt-5.5"}},
               profiles={"default": {"primary": "openai"}})

    result = migrate_model_refs(paths, apply=False)

    assert result["changed"] and result["applied"] is False
    assert read_yaml(tmp_path / "config.yaml")["models"]["defaults"]["provider"] == "openai-codex"


def test_the_rewrite_only_touches_reference_keys(tmp_path: Path, renames) -> None:
    """A blind string replace would corrupt prose that happens to contain the
    old provider name — an agent's role or description, for instance."""
    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "scribe.yaml", {
        "id": "scribe", "name": "scribe", "tools": [],
        "role": "Writes documentation about openai-codex and its history",
        "model": "openai-codex/gpt-5.5"})

    migrate_model_refs(paths, apply=True)

    written = read_yaml(paths.agents / "scribe.yaml")
    assert written["model"] == "openai/gpt-5.5"
    assert "openai-codex" in written["role"], "prose must not be rewritten"


def test_a_corrupt_state_file_does_not_break_the_migration(tmp_path: Path, renames) -> None:
    """One unreadable record is the recovery sweep's problem, not a reason to
    abandon the migration of everything else."""
    paths = init_runtime(tmp_path)
    _agent(paths, "writer", "openai-codex/gpt-5.5")
    (paths.sessions / "broken.json").write_text("{not json")

    result = migrate_model_refs(paths, apply=True)

    assert read_yaml(paths.agents / "writer.yaml")["model"] == "openai/gpt-5.5"
    assert all("broken.json" not in row["path"] for row in result["changed"])


# --- the doctor surface -----------------------------------------------------


def test_doctor_warns_and_offers_the_fix_for_renamed_refs(tmp_path: Path, renames) -> None:
    paths = init_runtime(tmp_path)
    _configure(tmp_path, defaults={"provider": "openai-codex"},
               providers={"openai": {"kind": "openai_compatible", "default_model": "gpt-5.5"}},
               profiles={"default": {"primary": "openai"}})

    check = doctor._check_model_refs(paths)

    assert check.status == doctor.WARN
    assert "renamed" in check.detail
    assert "doctor --fix" in (check.hint or "")


def test_the_fixer_applies_the_rename_end_to_end(tmp_path: Path, renames) -> None:
    paths = init_runtime(tmp_path)
    _configure(tmp_path, defaults={"provider": "openai-codex"},
               providers={"openai": {"kind": "openai_compatible", "default_model": "gpt-5.5"}},
               profiles={"default": {"primary": "openai"}})
    report = doctor.Report(checks=[doctor._check_model_refs(paths)])

    actions = doctor.run_fixes(paths, report)

    assert any(a["check"] == "model_refs" and a["fixed"] for a in actions)
    assert doctor._check_model_refs(paths).status == doctor.OK


def test_doctor_explains_the_silent_failure_for_a_dangling_ref(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _configure(tmp_path, providers={"chatgpt": {"kind": "chatgpt_oauth", "default_model": "gpt-5"}},
               profiles={"default": {"primary": "chatgpt"}})
    _agent(paths, "thrifty", "profile:cheap")

    check = doctor._check_model_refs(paths)

    assert check.status == doctor.WARN
    assert "falls back to the default" in (check.hint or "")

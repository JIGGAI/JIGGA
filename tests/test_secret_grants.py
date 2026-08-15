"""E1c: secrets released inside a capability invocation only per the executing
agent's grant; legacy installs (no secrets block) keep working with audited
visibility; enforce_grants flips the default to deny."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.secrets_broker import capability_secret_context, get_secret, set_secret


def _events(paths) -> list[dict]:
    log = paths.logs / "events.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def _agent(secrets_perm=None) -> AgentConfig:
    perms = {"secrets": secrets_perm} if secrets_perm is not None else {}
    return AgentConfig(id="worker", name="W", role="r", permissions=perms)


def test_granted_agent_reads_and_release_is_audited(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    set_secret(paths.home, "brave_api_key", "sv-9x7q-value")
    with capability_secret_context(_agent({"allow": ["brave_api_key"]}), paths.logs):
        assert get_secret(paths.home, "brave_api_key") == "sv-9x7q-value"
    released = [e for e in _events(paths) if e.get("type") == "secret.released"]
    assert released and released[-1]["details"]["granted"] is True
    assert released[-1]["details"]["name"] == "brave_api_key"
    assert "sv-9x7q-value" not in json.dumps(released)  # value never audited


def test_ungranted_name_denied_when_block_declared(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    set_secret(paths.home, "email_imap.json", "{}")
    with capability_secret_context(_agent({"allow": ["brave_api_key"]}), paths.logs):
        with pytest.raises(PermissionError, match="email_imap.json"):
            get_secret(paths.home, "email_imap.json")
    denied = [e for e in _events(paths) if e.get("type") == "secret.denied"]
    assert denied and denied[-1]["details"]["agent"] == "worker"


def test_legacy_agent_without_block_allowed_but_visible(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    set_secret(paths.home, "tok", "v")
    with capability_secret_context(_agent(None), paths.logs):
        assert get_secret(paths.home, "tok") == "v"
    released = [e for e in _events(paths) if e.get("type") == "secret.released"]
    assert released[-1]["details"]["granted"] is False  # the audit trail shows what grant to add


def test_enforce_grants_flips_legacy_to_deny(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.config, {"secrets": {"enforce_grants": True}})
    set_secret(paths.home, "tok", "v")
    with capability_secret_context(_agent(None), paths.logs):
        with pytest.raises(PermissionError):
            get_secret(paths.home, "tok")


def test_reads_outside_capability_context_are_unaffected(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    write_yaml(paths.config, {"secrets": {"enforce_grants": True}})
    set_secret(paths.home, "tok", "v")
    assert get_secret(paths.home, "tok") == "v"  # wizard/CLI/supervisor reads stay runtime-trusted


def test_dispatch_binds_the_context_end_to_end(tmp_path: Path) -> None:
    """A real capability call (email.search) through dispatch_action is denied
    the credential when the executing agent's secrets block excludes it."""
    from jigga.runtime.capabilities import CapabilityRegistry, load_capability_manifest, record_approval
    from jigga.runtime.dispatcher import dispatch_action
    from jigga.runtime.runtime_context import RuntimeContext
    from jigga.optional_capabilities import REGISTRY

    paths = init_runtime(tmp_path, examples=True)
    pack_dir = paths.capabilities / "email-imap"
    pack_dir.mkdir(parents=True)
    manifest_src = REGISTRY["email-imap"].manifest_path.read_text(encoding="utf-8")
    (pack_dir / "manifest.yaml").write_text(manifest_src, encoding="utf-8")
    record_approval(paths.policies, load_capability_manifest(pack_dir / "manifest.yaml"))
    registry = CapabilityRegistry.load(user_capabilities=paths.capabilities, approvals_dir=paths.policies)
    set_secret(paths.home, "email_imap.json", json.dumps(
        {"imap_host": "i", "smtp_host": "s", "username": "u", "password": "p"}))

    # Granted the tool, so the failure under test is the SECRET grant, not the
    # tool grant that now precedes it at dispatch.
    agent = AgentConfig(id="reader", name="R", role="r", tools=["email.search"],
                        permissions={"email": "read", "network": {"mode": "allow"},
                                     "secrets": {"allow": ["something_else"]}})
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    with pytest.raises(PermissionError, match="email_imap.json"):
        dispatch_action(WorkflowStep(id="s", action="email.search"), {"filters": ["unread"]},
                        {}, runtime, registry, paths.logs, run_id="r1")

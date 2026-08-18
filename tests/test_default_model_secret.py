"""An agent may use the model it is configured to use.

`secrets.enforce_grants: true` denied any secret to an agent with no
`permissions.secrets` block — including the credential for the DEFAULT model
provider. So a scaffolded agent could not call its own LLM, which is not an
agent, and `team_launch` failed with "Secret 'chatgpt_auth.json' is not granted
to agent 'marketing_lead'".

Worse, the same credential was governed differently depending on how it was
reached: an agent's OWN model call never passes through the capability secret
scope, so `chief` chatted all night with no chatgpt grant while a workflow step
was refused. Two answers to one question.

This grants no new power — the unchecked path already existed. It makes the
checked one agree with it, and keeps the grant where it is a real escalation:
another provider's credential, or any non-model secret.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.core.models import AgentConfig
from jigga.runtime.secrets_broker import (
    capability_secret_context,
    default_model_secret,
    get_secret,
    set_secret,
)


def _chatgpt_runtime(tmp_path: Path) -> None:
    """A home whose default provider is the ChatGPT OAuth subscription."""
    paths = init_runtime(tmp_path)
    config = read_yaml(paths.home / "config.yaml")
    config["models"] = {
        "defaults": {"provider": "chatgpt"},
        "providers": {"chatgpt": {"kind": "chatgpt_oauth", "default_model": "gpt-5"},
                      "other": {"kind": "openai_compatible", "default_model": "x"}},
    }
    config["secrets"] = {"enforce_grants": True}
    write_yaml(paths.home / "config.yaml", config)
    set_secret(paths.home, "chatgpt_auth.json", '{"tokens": {}}')
    set_secret(paths.home, "telegram_bot_token", "t0ken")
    set_secret(paths.home, "other_api_key", "k")


def _agent(permissions: dict | None = None) -> AgentConfig:
    return AgentConfig(id="a", name="A", role="r", permissions=permissions or {})


def test_the_default_providers_credential_needs_no_grant(tmp_path: Path) -> None:
    _chatgpt_runtime(tmp_path)
    with capability_secret_context(_agent(), tmp_path / "logs"):
        assert get_secret(tmp_path, "chatgpt_auth.json")


def test_it_resolves_which_secret_that_is_from_the_model_config(tmp_path: Path) -> None:
    _chatgpt_runtime(tmp_path)
    assert default_model_secret(tmp_path) == "chatgpt_auth.json"


def test_an_agent_with_an_unrelated_secrets_block_can_still_think(tmp_path: Path) -> None:
    # `chief` looked like this: a block naming the telegram token only. Reading
    # that as "denied the model" is how a chief of staff loses the ability to
    # answer you.
    _chatgpt_runtime(tmp_path)
    agent = _agent({"secrets": {"allow": ["telegram_bot_token"]}})
    with capability_secret_context(agent, tmp_path / "logs"):
        assert get_secret(tmp_path, "chatgpt_auth.json")


def test_another_providers_credential_still_needs_a_grant(tmp_path: Path) -> None:
    # Switching provider means a different account and a different bill — that
    # is the escalation worth asking about.
    _chatgpt_runtime(tmp_path)
    with capability_secret_context(_agent(), tmp_path / "logs"), pytest.raises(PermissionError):
        get_secret(tmp_path, "other_api_key")


def test_a_non_model_secret_still_needs_a_grant(tmp_path: Path) -> None:
    _chatgpt_runtime(tmp_path)
    with capability_secret_context(_agent(), tmp_path / "logs"), pytest.raises(PermissionError):
        get_secret(tmp_path, "telegram_bot_token")


def test_an_explicit_deny_still_wins(tmp_path: Path) -> None:
    # Naming a secret in `deny` is a deliberate act; the silence of never having
    # written a block is not. Only the former overrides the default.
    _chatgpt_runtime(tmp_path)
    agent = _agent({"secrets": {"deny": ["chatgpt_auth.json"]}})
    with capability_secret_context(agent, tmp_path / "logs"), pytest.raises(PermissionError):
        get_secret(tmp_path, "chatgpt_auth.json")


def test_the_release_is_audited_with_its_reason(tmp_path: Path, capsys) -> None:
    import json

    from jigga.cli import main

    _chatgpt_runtime(tmp_path)
    with capability_secret_context(_agent(), tmp_path / "logs"):
        get_secret(tmp_path, "chatgpt_auth.json")
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "audit", "--type", "secret.released", "--json"]) == 0
    events = json.loads(capsys.readouterr().out)
    assert events[-1]["details"]["reason"] == "default model provider"
    assert events[-1]["details"]["granted"] is True


def test_a_runtime_with_no_model_config_has_no_implicit_secret(tmp_path: Path) -> None:
    # Nothing to be implicit ABOUT: don't invent a grant for a provider that is
    # not configured.
    init_runtime(tmp_path)
    assert default_model_secret(tmp_path) is None

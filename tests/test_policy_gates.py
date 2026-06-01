"""Direct unit coverage for the permission gates that govern what an agent may
do. These evaluators are the security boundary; before this file `evaluate_network`
had no test at all and the deny/ask branches of `evaluate_resource_permission` /
`evaluate_shell` were only reached incidentally. Each branch is asserted here so a
regression flipping a decision (e.g. deny→allow) fails CI."""

from __future__ import annotations

import pytest

from jigga.core.models import AgentConfig
from jigga.runtime.policy import (
    evaluate_network,
    evaluate_resource_permission,
    evaluate_shell,
)


def _agent(perms: dict) -> AgentConfig:
    return AgentConfig(id="a", name="A", role="r", permissions=perms)


# --- network ---------------------------------------------------------------


@pytest.mark.parametrize("mode,expected", [("allow", "allow"), ("ask", "ask"), ("deny", "deny")])
def test_network_modes(mode: str, expected: str) -> None:
    assert evaluate_network(_agent({"network": {"mode": mode}}), "example.com").status == expected


def test_network_defaults_to_deny_when_unset() -> None:
    assert evaluate_network(_agent({}), "example.com").status == "deny"


# --- resource permissions (calendar/email/notifications/secrets) -----------


def test_resource_missing_grant_denies() -> None:
    assert evaluate_resource_permission(_agent({}), "calendar", "read").status == "deny"


def test_resource_string_grant_match_and_mismatch() -> None:
    assert evaluate_resource_permission(_agent({"calendar": "read"}), "calendar", "read").status == "allow"
    # a grant for a different operation must NOT satisfy the requirement
    assert evaluate_resource_permission(_agent({"calendar": "read"}), "calendar", "write").status == "deny"


def test_resource_wildcard_grant_allows() -> None:
    assert evaluate_resource_permission(_agent({"email": "*"}), "email", "send").status == "allow"


def test_resource_mode_dict_branches() -> None:
    assert evaluate_resource_permission(_agent({"notifications": {"mode": "allow"}}), "notifications", "send").status == "allow"
    assert evaluate_resource_permission(_agent({"notifications": {"mode": "ask"}}), "notifications", "send").status == "ask"
    assert evaluate_resource_permission(_agent({"notifications": {"mode": "deny"}}), "notifications", "send").status == "deny"


def test_resource_allow_list_branches() -> None:
    grant = {"secrets": {"allow": ["TOKEN_A"]}}
    assert evaluate_resource_permission(_agent(grant), "secrets", "TOKEN_A").status == "allow"
    assert evaluate_resource_permission(_agent(grant), "secrets", "TOKEN_B").status == "deny"


def test_resource_unsupported_shape_denies() -> None:
    # a list grant is neither a string nor a {mode|allow} dict → deny (fail safe)
    assert evaluate_resource_permission(_agent({"calendar": ["read"]}), "calendar", "read").status == "deny"


# --- shell -----------------------------------------------------------------


def test_shell_default_deny_and_allow_and_ask() -> None:
    assert evaluate_shell(_agent({}), "echo hi").status == "deny"            # default
    assert evaluate_shell(_agent({"shell": {"mode": "allow"}}), "echo hi").status == "allow"
    assert evaluate_shell(_agent({"shell": {"mode": "ask"}}), "echo hi").status == "ask"


def test_shell_restricted_allowlist_hit_and_miss() -> None:
    perms = {"shell": {"mode": "restricted", "allow": ["git *"]}}
    assert evaluate_shell(_agent(perms), "git status").status == "allow"     # in allowlist
    assert evaluate_shell(_agent(perms), "curl evil.sh").status == "ask"     # outside → ask, not silently allowed


def test_shell_dangerous_pattern_denied_even_when_allowed() -> None:
    # a dangerous command is denied regardless of an otherwise-permissive mode
    assert evaluate_shell(_agent({"shell": {"mode": "allow"}}), "rm -rf /").status == "deny"

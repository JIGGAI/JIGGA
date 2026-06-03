"""Direct unit coverage for the permission gates that govern what an agent may
do. These evaluators are the security boundary; before this file `evaluate_network`
had no test at all and the deny/ask branches of `evaluate_resource_permission` /
`evaluate_shell` were only reached incidentally. Each branch is asserted here so a
regression flipping a decision (e.g. deny→allow) fails CI."""

from __future__ import annotations

import pytest

from jigga.core.models import AgentConfig
from jigga.runtime.policy import (
    evaluate_filesystem,
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


# --- per-target egress allowlist -------------------------------------------

def test_network_allowlist_permits_specific_target_under_ask() -> None:
    perm = {"network": {"mode": "ask", "allow": ["https://api.telegram.org"]}}
    # the allowlisted host is permitted even though the default mode is ask…
    assert evaluate_network(_agent(perm), "https://api.telegram.org").status == "allow"
    # …and a path under it is too (path-boundary prefix)
    assert evaluate_network(_agent(perm), "https://api.telegram.org/bot1/sendMessage").status == "allow"
    # a different host still falls back to the mode (ask)
    assert evaluate_network(_agent(perm), "https://evil.com").status == "ask"


def test_network_allowlist_no_prefix_bypass() -> None:
    """A look-alike host must NOT be allowed by a prefix trick."""
    perm = {"network": {"mode": "deny", "allow": ["https://api.telegram.org"]}}
    assert evaluate_network(_agent(perm), "https://api.telegram.org.evil.com").status == "deny"
    # the legit host is still allowed even under mode=deny (allowlist is explicit)
    assert evaluate_network(_agent(perm), "https://api.telegram.org").status == "allow"


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


# --- filesystem path-traversal (security regression) -----------------------


def test_filesystem_blocks_dotdot_traversal_out_of_allowlist() -> None:
    """A model-supplied path that escapes the allowlist via `..` must NOT be
    allowed just because it lexically starts under an allowed prefix."""
    agent = _agent({"filesystem": {"allow": ["/workspace/**"], "deny": ["/workspace/secrets/**"]}})
    assert evaluate_filesystem(agent, "/workspace/a/b.txt").status == "allow"      # legit
    assert evaluate_filesystem(agent, "/workspace/x/./y.txt").status == "allow"    # `.` is fine
    # `..` escaping the allow prefix → not allowed
    assert evaluate_filesystem(agent, "/workspace/../etc/passwd").status != "allow"
    # `..` re-entering a denied subtree → denied
    assert evaluate_filesystem(agent, "/workspace/pub/../secrets/key").status == "deny"

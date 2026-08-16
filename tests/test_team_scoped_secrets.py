"""Two tenants on one runtime need different values for the same logical secret.

From the field lessons: Woods split into two venues, Oakwood and Driftwood,
whose brand rules are *mutually exclusive* — Driftwood is defined by a water
view, Oakwood must never imply one. Each venue has its own team, social
accounts, and DB.

> per-team config isolation isn't a nice-to-have, it's a correctness
> constraint

> a second customer arrives as a *fork* unless per-team config is genuinely
> complete

The secret namespace was flat and handlers resolved literal names — there was
exactly one such call site (`web.py`: `get_secret(home, "brave_api_key")`).
With one Postiz login per venue, a handler asking for `postiz_api_key` would
have had to be forked per tenant. Cheap to fix at one call site; a migration
after the HMX and Woods recipes add more.

The property that matters: isolation is **structural**. An agent's team decides
which value it sees, so granting an Oakwood agent the logical name still cannot
reach Driftwood's credential.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig
from jigga.runtime.secrets_broker import (
    capability_secret_context,
    get_secret,
    resolve_secret_name,
    set_secret,
    team_of,
)


def _team(paths, team_id: str, *members: str) -> None:
    write_yaml(paths.teams / f"{team_id}.yaml", {
        "id": team_id, "name": team_id,
        "agents": [{"id": member} for member in members]})


def _agent(agent_id: str) -> AgentConfig:
    return AgentConfig(id=agent_id, name=agent_id, role="r", tools=[])


def _read_as(home: Path, agent: AgentConfig, name: str, logs: Path) -> str | None:
    with capability_secret_context(agent, logs):
        return get_secret(home, name)


# --- the venues ---------------------------------------------------------------


@pytest.fixture
def venues(tmp_path: Path):
    """Two teams, one logical secret, two different accounts behind it."""
    paths = init_runtime(tmp_path)
    _team(paths, "oakwood", "oak_writer")
    _team(paths, "driftwood", "drift_writer")
    set_secret(tmp_path, "postiz_api_key@oakwood", "OAK-TOKEN")
    set_secret(tmp_path, "postiz_api_key@driftwood", "DRIFT-TOKEN")
    return paths


def test_each_venue_gets_its_own_credential(venues) -> None:
    home = venues.home
    assert _read_as(home, _agent("oak_writer"), "postiz_api_key", venues.logs) == "OAK-TOKEN"
    assert _read_as(home, _agent("drift_writer"), "postiz_api_key", venues.logs) == "DRIFT-TOKEN"


def test_a_grant_cannot_reach_across_teams(venues) -> None:
    """The isolation is structural, not a grant. Even an agent explicitly
    granted the logical name sees only its own team's value — which is what
    makes mutually-exclusive tenants safe on one box."""
    home = venues.home
    generous = AgentConfig(id="oak_writer", name="oak", role="r", tools=[],
                           permissions={"secrets": {"allow": ["postiz_api_key"]}})
    assert _read_as(home, generous, "postiz_api_key", venues.logs) == "OAK-TOKEN"


def test_the_handler_asks_for_one_name_and_needs_no_tenant_awareness(venues) -> None:
    """The whole point: a capability stays single-tenant in its own code. If it
    had to know about venues, every capability would need forking."""
    home = venues.home
    seen = {agent: _read_as(home, _agent(agent), "postiz_api_key", venues.logs)
            for agent in ("oak_writer", "drift_writer")}
    assert len(set(seen.values())) == 2, "one literal name, two values"


# --- single-tenant installs must not change ----------------------------------


def test_an_unscoped_secret_still_resolves(tmp_path: Path) -> None:
    """The overwhelmingly common case: no teams, no scoping, unchanged."""
    paths = init_runtime(tmp_path)
    set_secret(tmp_path, "brave_api_key", "PLAIN")
    assert _read_as(tmp_path, _agent("solo"), "brave_api_key", paths.logs) == "PLAIN"


def test_a_team_agent_falls_back_to_the_unscoped_secret(tmp_path: Path) -> None:
    """A team that hasn't set a scoped value keeps using the shared one, so
    adopting teams doesn't silently break existing credentials."""
    paths = init_runtime(tmp_path)
    _team(paths, "oakwood", "oak_writer")
    set_secret(tmp_path, "brave_api_key", "SHARED")
    assert _read_as(tmp_path, _agent("oak_writer"), "brave_api_key", paths.logs) == "SHARED"


def test_a_scoped_value_overrides_the_shared_one(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, "oakwood", "oak_writer")
    set_secret(tmp_path, "brave_api_key", "SHARED")
    set_secret(tmp_path, "brave_api_key@oakwood", "OAK-ONLY")
    assert _read_as(tmp_path, _agent("oak_writer"), "brave_api_key", paths.logs) == "OAK-ONLY"


def test_reads_outside_a_capability_are_not_scoped(tmp_path: Path) -> None:
    """Login wizards, the CLI and the supervisor's own polling are
    runtime-trusted and have no executing agent to scope by."""
    init_runtime(tmp_path)
    set_secret(tmp_path, "brave_api_key", "PLAIN")
    assert get_secret(tmp_path, "brave_api_key") == "PLAIN"


# --- name resolution ----------------------------------------------------------


def test_an_explicit_scoped_name_is_taken_literally(venues) -> None:
    """An operator naming a tenant explicitly means it — used by the CLI and
    by anything deliberately administering another team's credential."""
    assert resolve_secret_name(venues.home, "postiz_api_key@driftwood",
                               _agent("oak_writer")) == "postiz_api_key@driftwood"


def test_resolution_falls_back_when_no_scoped_value_exists(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, "oakwood", "oak_writer")
    assert resolve_secret_name(tmp_path, "nothing_here", _agent("oak_writer")) == "nothing_here"


def test_an_agent_on_no_team_resolves_unscoped(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    assert resolve_secret_name(tmp_path, "brave_api_key", _agent("nomad")) == "brave_api_key"


def test_team_lookup_finds_membership(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _team(paths, "oakwood", "oak_writer", "oak_designer")
    assert team_of(tmp_path, _agent("oak_designer")) == "oakwood"
    assert team_of(tmp_path, _agent("stranger")) is None


def test_an_unreadable_team_file_does_not_block_a_secret_read(tmp_path: Path) -> None:
    """A broken team file is a config problem; it must not take credentials
    down with it."""
    paths = init_runtime(tmp_path)
    (paths.teams / "wrecked.yaml").write_text("{[not yaml\n")
    set_secret(tmp_path, "brave_api_key", "PLAIN")
    assert _read_as(tmp_path, _agent("solo"), "brave_api_key", paths.logs) == "PLAIN"


def test_scoped_names_are_valid_to_store_and_reject_nonsense(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    set_secret(tmp_path, "key@team", "v")          # accepted
    assert get_secret(tmp_path, "key@team") == "v"
    with pytest.raises(ValueError):
        set_secret(tmp_path, "key@team@extra", "v")   # one scope, not a path
    with pytest.raises(ValueError):
        set_secret(tmp_path, "../escape", "v")

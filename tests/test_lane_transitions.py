from __future__ import annotations

from jigga.core.models import TeamConfig
from jigga.runtime.lanes import derive_lane, lane_transitions, role_of

PIPELINE_TRANSITIONS = {
    "rules": [
        {"from": "lead", "to": "dev", "lane": "in-progress"},
        {"from": "dev", "to": "test", "lane": "testing"},
        {"from": "test", "to": "dev", "lane": "in-progress"},
        {"from": "test", "to": "lead", "lane": "ready-for-pr"},
    ],
    "bounce_lane": "backlog",
}

ENG_LANES = [
    {"id": "backlog"}, {"id": "in-progress"},
    {"id": "testing", "gate": "test"},
    {"id": "ready-for-pr", "gate": "lead"},
    {"id": "done"},
]
AGENTS = [
    {"id": "eng-lead", "role": "lead"},
    {"id": "eng-dev", "role": "dev"},
    {"id": "eng-test", "role": "test"},
]


def _team(lanes=None, transitions=PIPELINE_TRANSITIONS) -> TeamConfig:
    """A board's shape is the team's to declare — core supplies none."""
    data = {"id": "eng", "name": "Eng", "agents": AGENTS, "lanes": lanes or ENG_LANES}
    if transitions is not None:
        data["lane_transitions"] = transitions
    return TeamConfig.from_dict(data)


def test_role_of_reads_the_team_roster() -> None:
    team = _team()
    assert role_of(team, "eng-dev") == "dev"
    assert role_of(team, "nobody") is None


def test_a_declared_table_drives_the_standard_pipeline() -> None:
    team = _team()
    assert derive_lane(team, "eng-lead", "eng-dev") == "in-progress"
    assert derive_lane(team, "eng-dev", "eng-test") == "testing"
    assert derive_lane(team, "eng-test", "eng-lead") == "ready-for-pr"


def test_qa_rejection_returns_the_ticket_to_the_author() -> None:
    # A rejected ticket must go back to in-progress, not fall through to the
    # bounce lane, which would lose the fact that QA actively sent it back.
    team = _team()
    assert derive_lane(team, "eng-test", "eng-dev") == "in-progress"


def test_an_undeclared_transition_derives_nothing() -> None:
    team = _team()
    assert derive_lane(team, "eng-dev", "eng-lead") is None


def test_a_team_may_declare_its_own_rules() -> None:
    team = _team(transitions={"rules": [{"from": "lead", "to": "dev", "lane": "testing"}],
                              "bounce_lane": "backlog"})
    assert derive_lane(team, "eng-lead", "eng-dev") == "testing"
    assert derive_lane(team, "eng-dev", "eng-test") is None   # only what was declared
    assert lane_transitions(team)["bounce_lane"] == "backlog"


def test_rules_naming_a_lane_the_board_lacks_are_dropped() -> None:
    # A team whose board has no `testing` lane must not be handed one.
    team = _team(lanes=[{"id": "backlog"}, {"id": "in-progress"}, {"id": "done"}])
    assert derive_lane(team, "eng-dev", "eng-test") is None
    assert derive_lane(team, "eng-lead", "eng-dev") == "in-progress"


def test_the_declared_bounce_lane_must_exist_on_the_board() -> None:
    assert lane_transitions(_team())["bounce_lane"] == "backlog"
    assert lane_transitions(_team(lanes=[{"id": "a"}, {"id": "done"}]))["bounce_lane"] is None


def test_a_team_that_declares_nothing_gets_nothing() -> None:
    """Core used to supply one team's pipeline as the default, which handed
    every other board a table full of lanes it does not have."""
    bare = _team(transitions=None)
    assert lane_transitions(bare) == {"rules": [], "bounce_lane": None}
    assert derive_lane(bare, "eng-lead", "eng-dev") is None

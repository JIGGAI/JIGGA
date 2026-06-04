from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.core.models import AgentConfig, TeamConfig
from jigga.runtime.validation import (
    validate_agent,
    validate_configs,
    validate_cron,
    validate_team,
)


@pytest.mark.parametrize("cron", [
    "0 9 * * *", "*/15 * * * *", "30 7 * * 1-5", "0 9 * * MON-FRI",
    "0 9 * * MON,WED,FRI", "0 9 * * 7", "* * * * *",
])
def test_valid_cron(cron: str) -> None:
    assert validate_cron(cron) is None


@pytest.mark.parametrize("cron", [
    "x * * * *", "*/0 * * * *", "60 * * * *", "* * * *", "0 25 * * *", "0 9 * * 8",
])
def test_invalid_cron(cron: str) -> None:
    assert validate_cron(cron) is not None


def test_validate_agent_flags_bad_schedule_cron() -> None:
    agent = AgentConfig(id="a", name="A", role="r", wake={"schedules": [{"cron": "*/0 * * * *"}]})
    problems = validate_agent(agent)
    assert problems and "wake.schedules[0]" in problems[0]
    ok = AgentConfig(id="b", name="B", role="r", wake={"schedules": [{"cron": "0 9 * * 1-5"}]})
    assert validate_agent(ok) == []


def test_validate_team_handoffs() -> None:
    members = [{"id": "lead"}, {"id": "writer"}]
    good = TeamConfig(id="t", name="T", agents=members,
                      routing={"handoffs": [{"from": "lead", "to": "writer", "when": "ready"}]})
    assert validate_team(good) == []

    missing = TeamConfig(id="t", name="T", agents=members,
                         routing={"handoffs": [{"from": "lead"}]})
    assert any("missing 'to'" in p for p in validate_team(missing))

    non_member = TeamConfig(id="t", name="T", agents=members,
                            routing={"handoffs": [{"from": "lead", "to": "ghost"}]})
    probs = validate_team(non_member)
    assert any(p.startswith("warning:") and "ghost" in p for p in probs)   # warning, not error

    bad_shape = TeamConfig(id="t", name="T", agents=members, routing={"handoffs": {"oops": 1}})
    assert any("must be a list" in p for p in validate_team(bad_shape))

    bad_routing = TeamConfig(id="t", name="T", agents=members)
    bad_routing.routing = "not-a-dict"
    assert any("routing must be a mapping" in p for p in validate_team(bad_routing))


def test_jigga_validate_reports_problems_and_exit_code(tmp_path: Path, capsys) -> None:
    from jigga.cli import main
    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "bad.yaml", {"id": "bad", "name": "Bad", "role": "x",
               "wake": {"schedules": [{"cron": "x * * * *"}]}})
    rc = main(["--home", str(tmp_path), "validate"])
    out = capsys.readouterr().out
    assert rc == 1                                   # error-level problem → non-zero exit
    assert "problems:" in out and "bad" in out and "wake.schedules[0]" in out


def test_scaffold_fails_fast_on_bad_cron(tmp_path: Path) -> None:
    from jigga.runtime.recipes import Recipe, scaffold_agent
    paths = init_runtime(tmp_path)
    recipe = Recipe(id="r", name="R", kind="agent",
                    meta={"agent": {"cronJobs": [{"schedule": "*/0 * * * *", "message": "loop"}]}})
    with pytest.raises(ValueError, match="invalid cron"):
        scaffold_agent(paths.home, recipe, agent_id="r", agents_dir=paths.agents)


def test_validate_configs_aggregates() -> None:
    agents = {"a": AgentConfig(id="a", name="A", role="r", wake={"schedules": [{"cron": "bad"}]})}
    teams = {"t": TeamConfig(id="t", name="T", agents=[{"id": "x"}],
                             routing={"handoffs": [{"from": "x"}]})}
    problems = validate_configs(agents, teams)
    assert any("agent a" in p for p in problems) and any("team t" in p for p in problems)

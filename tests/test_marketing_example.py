from __future__ import annotations

from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_workflows
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.workflow import plan_workflow


def test_marketing_team_example_is_installed_and_valid(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)

    agents = load_agents(paths.agents)
    for aid in ("marketing_lead", "copywriter", "seo_editor"):
        assert aid in agents, f"{aid} example agent missing"

    assert (paths.teams / "marketing_team.yaml").exists()

    workflows = load_workflows(paths.workflows)
    assert "team_launch" in workflows
    wf = workflows["team_launch"]
    assert [s.action for s in wf.steps] == ["draft_with_model", "draft_with_model", "draft_with_model"]

    # The workflow plans cleanly: every step's agent exists and draft_with_model
    # resolves + is allowed (low risk, no special permission).
    reg = CapabilityRegistry.load(user_capabilities=paths.capabilities, approvals_dir=tmp_path / "policies")
    plan = plan_workflow(wf, agents, default_mode="ask", registry=reg)
    assert plan["can_run"], plan

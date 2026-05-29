from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_workflows
from jigga.core.models import AgentConfig
from jigga.runtime.policy import evaluate_filesystem, evaluate_shell
from jigga.runtime.workflow import plan_workflow, run_workflow
from jigga.tools.safe_process import ProcessPolicyError, run_safe_process


def test_policy_denies_shell_by_default_and_dangerous_patterns(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = load_agents(paths.agents)["content_strategist"]
    assert evaluate_shell(agent, "echo hello").status == "deny"
    assert evaluate_shell(agent, "rm -rf /tmp/example").status == "deny"


def test_policy_flags_filesystem_outside_allow_list_for_approval(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = load_agents(paths.agents)["content_strategist"]
    allowed = evaluate_filesystem(agent, "~/Projects/content/brief.md", "read")
    outside = evaluate_filesystem(agent, tmp_path / "outside.md", "read")
    denied = evaluate_filesystem(agent, "~/.ssh/id_rsa", "read")
    assert allowed.status == "allow"
    assert outside.status == "ask"
    assert denied.status == "deny"


def test_workflow_required_approval_returns_needs_approval(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    workflow = load_workflows(paths.workflows)["social_content_syndication"]
    plan = plan_workflow(workflow, load_agents(paths.agents))
    assert plan["can_run"] is False
    assert any(step["policy"]["status"] == "needs_approval" for step in plan["steps"])
    result = run_workflow(paths.home, paths.logs, paths.workflows, paths.agents, paths.memory, "social_content_syndication")
    assert result["status"] == "needs_approval"


def test_filesystem_deny_matches_bare_basename_anywhere_in_tree() -> None:
    agent = AgentConfig(
        id="sentry",
        name="Sentry",
        role="secrets guard",
        permissions={"filesystem": {"allow": ["/workspace"], "deny": [".env", "id_rsa", "secrets/**"]}},
    )
    # bare basenames must match the file no matter how deeply nested
    assert evaluate_filesystem(agent, "/workspace/.env").status == "deny"
    assert evaluate_filesystem(agent, "/workspace/apps/api/.env").status == "deny"
    assert evaluate_filesystem(agent, "/workspace/apps/api/config/id_rsa").status == "deny"
    # `**` glob must match files under the prefix
    assert evaluate_filesystem(agent, "/workspace/secrets/private.key").status == "deny"
    # similar-looking names must NOT trigger a false match
    assert evaluate_filesystem(agent, "/workspace/apps/foo.env.example").status == "allow"
    assert evaluate_filesystem(agent, "/workspace/apps/.envrc").status == "allow"


def test_filesystem_deny_handles_tilde_directory_patterns() -> None:
    agent = AgentConfig(
        id="sentry",
        name="Sentry",
        role="secrets guard",
        permissions={"filesystem": {"allow": ["~/Projects"], "deny": ["~/.ssh", "~/.aws"]}},
    )
    assert evaluate_filesystem(agent, "~/.ssh/id_rsa").status == "deny"
    assert evaluate_filesystem(agent, "~/.aws/credentials").status == "deny"
    assert evaluate_filesystem(agent, "~/Projects/content/brief.md").status == "allow"


def test_safe_process_plans_allowed_command_and_blocks_dangerous_command(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = load_agents(paths.agents)["daily_briefing_agent"]
    agent.permissions["shell"] = {"mode": "restricted", "allow": ["python --version"]}
    agent.permissions["filesystem"] = {"allow": [str(tmp_path)]}

    planned = run_safe_process(agent, ["python", "--version"], tmp_path, paths.runs, apply=False)
    assert planned["status"] == "planned"
    assert planned["dry_run"] is True

    with pytest.raises(ProcessPolicyError):
        run_safe_process(agent, ["rm", "-rf", str(tmp_path)], tmp_path, paths.runs, apply=False)

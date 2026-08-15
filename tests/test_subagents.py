from __future__ import annotations

from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.config import load_agents, load_workflows
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.subagents import SpawnSubagentInput, SubagentSession, _restricted_env, _truncate_summary, list_sessions, read_session, spawn_subagent, write_session
from jigga.runtime.workflow import plan_workflow, run_workflow


def _enable_content_delegation(paths) -> None:
    # Example content_strategist ships delegation enabled; this helper documents intent.
    assert load_agents(paths.agents)["content_strategist"].delegation["enabled"] is True


def _spawn_payload(**overrides):
    payload = {
        "backend": "dry_run",
        "mode": "plan",
        "parent_agent_id": "content_strategist",
        "task_id": "task_demo",
        "work_order": {"goal": "Inspect the content plan", "instructions": "Return risks."},
        "cwd": "~/Projects/content",
        "memory_scope": "task_context_only",
        "limits": {"max_runtime_minutes": 1},
        "output_required": ["summary", "risks"],
    }
    payload.update(overrides)
    return payload


def test_spawn_input_validates_work_order_goal() -> None:
    try:
        SpawnSubagentInput.from_dict({"parent_agent_id": "a", "work_order": {}})
    except ValueError as exc:
        assert "goal" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected goal validation failure")


def test_delegation_disabled_blocks(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = load_agents(paths.agents)["daily_briefing_agent"]
    try:
        spawn_subagent(paths.home, paths.logs, paths.sessions, agent, _spawn_payload(parent_agent_id=agent.id))
    except ValueError as exc:
        assert "not allowed to delegate" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected delegation policy failure")


def test_dry_run_spawn_persists_completed_session(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    _enable_content_delegation(paths)
    agent = load_agents(paths.agents)["content_strategist"]
    session = spawn_subagent(paths.home, paths.logs, paths.sessions, agent, _spawn_payload())
    assert session.status == "completed"
    assert session.backend == "dry_run"
    assert "Dry-run subagent planned" in session.result["summary"]
    assert Path(session.logs_path).exists()
    assert list_sessions(paths.sessions)[0].id == session.id


def test_backend_allowlist_blocks_codex_by_default(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = load_agents(paths.agents)["content_strategist"]
    try:
        spawn_subagent(paths.home, paths.logs, paths.sessions, agent, _spawn_payload(backend="codex_cli"))
    except ValueError as exc:
        assert "not allowed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected backend allowlist failure")


def test_backend_allowlist_blocks_claude_code_by_default(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = load_agents(paths.agents)["content_strategist"]
    try:
        spawn_subagent(paths.home, paths.logs, paths.sessions, agent, _spawn_payload(backend="claude_code"))
    except ValueError as exc:
        assert "not allowed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected backend allowlist failure")


def test_claude_code_requires_global_flag_even_when_on_agent_allowlist(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    # Add claude_code to the agent's allowed_backends; the global flag should
    # still be required.
    agent_yaml = paths.agents / "content_strategist.yaml"
    agent_doc = read_yaml(agent_yaml)
    agent_doc["delegation"]["allowed_backends"].append("claude_code")
    write_yaml(agent_yaml, agent_doc)
    agent = load_agents(paths.agents)["content_strategist"]
    try:
        spawn_subagent(paths.home, paths.logs, paths.sessions, agent, _spawn_payload(backend="claude_code"))
    except ValueError as exc:
        assert "claude_code_enabled" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected global flag failure")


def test_depth_limit_is_enforced(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = load_agents(paths.agents)["content_strategist"]
    try:
        spawn_subagent(paths.home, paths.logs, paths.sessions, agent, _spawn_payload(depth=2))
    except ValueError as exc:
        assert "max_depth" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected max_depth failure")


def test_spawn_subagent_capability_is_registered() -> None:
    registry = CapabilityRegistry.load()
    capability = registry.resolve_action("spawn_subagent")
    assert capability is not None
    assert capability.handler == "runtime.spawn_subagent"
    assert capability.risk_level == "medium"


def test_workflow_dispatch_spawns_subagent(tmp_path: Path, grant) -> None:
    paths = init_runtime(tmp_path, examples=True)
    grant(paths, "content_strategist", "spawn_subagent")
    write_yaml(
        paths.workflows / "delegate.yaml",
        {
            "id": "delegate",
            "name": "Delegate",
            "steps": [
                {
                    "id": "spawn",
                    "agent": "content_strategist",
                    "action": "spawn_subagent",
                    "input": _spawn_payload(),
                    "output": "subagent.json",
                }
            ],
        },
    )
    workflow = load_workflows(paths.workflows)["delegate"]
    plan = plan_workflow(
        workflow,
        load_agents(paths.agents),
        default_mode="autonomous",
        registry=CapabilityRegistry.load(user_capabilities=paths.capabilities),
    )
    assert plan["can_run"] is True
    agent_yaml = paths.agents / "content_strategist.yaml"
    agent_yaml.write_text(agent_yaml.read_text(encoding="utf-8") + "\npermission_mode: autonomous\n", encoding="utf-8")
    result = run_workflow(paths, "delegate")
    assert result["status"] == "completed"
    assert result["outputs"]["spawn"]["status"] == "completed"
    assert list_sessions(paths.sessions)


def test_sessions_cli_smoke(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = load_agents(paths.agents)["content_strategist"]
    session = spawn_subagent(paths.home, paths.logs, paths.sessions, agent, _spawn_payload())
    assert main(["--home", str(tmp_path), "sessions", "list"]) == 0
    assert session.id in capsys.readouterr().out
    assert main(["--home", str(tmp_path), "sessions", "inspect", session.id[:14]]) == 0
    assert '"status": "completed"' in capsys.readouterr().out



def test_subagent_cwd_must_be_allowed_by_parent_policy(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = load_agents(paths.agents)["content_strategist"]
    try:
        spawn_subagent(paths.home, paths.logs, paths.sessions, agent, _spawn_payload(cwd="/outside"))
    except ValueError as exc:
        assert "outside allow list" in str(exc) or "no allow list" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected cwd policy failure")


def test_subagent_declared_filesystem_permissions_must_fit_parent_policy(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = load_agents(paths.agents)["content_strategist"]
    try:
        spawn_subagent(
            paths.home,
            paths.logs,
            paths.sessions,
            agent,
            _spawn_payload(cwd="~/Projects/content", permissions={"filesystem": {"write": ["/outside"]}}),
        )
    except ValueError as exc:
        assert "Filesystem write" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected filesystem permission failure")


def test_subagent_can_narrow_scope_with_deny_rules(tmp_path: Path) -> None:
    # A subagent voluntarily denying a subpath of the parent's allow is the
    # canonical "stricter than parent" pattern from the spec. Earlier policy
    # erroneously rejected this. The session should be accepted.
    paths = init_runtime(tmp_path, examples=True)
    agent = load_agents(paths.agents)["content_strategist"]
    session = spawn_subagent(
        paths.home,
        paths.logs,
        paths.sessions,
        agent,
        _spawn_payload(
            cwd="~/Projects/content",
            permissions={"filesystem": {"deny": ["~/Projects/content/secrets"]}},
        ),
    )
    assert session.status == "completed"


def test_restricted_env_excludes_unrequested_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/bin")
    request = SpawnSubagentInput.from_dict(_spawn_payload(permissions={"secrets": {"required": []}}))
    env = _restricted_env(request)
    assert env["PATH"] == "/bin"
    assert "OPENAI_API_KEY" not in env


def test_restricted_env_includes_explicitly_requested_secret(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    request = SpawnSubagentInput.from_dict(_spawn_payload(permissions={"secrets": {"required": ["OPENAI_API_KEY"]}}))
    assert _restricted_env(request)["OPENAI_API_KEY"] == "secret"


def test_read_session_prefers_exact_and_rejects_ambiguous_prefix(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    first = SubagentSession("subagent_abc111", "dry_run", "plan", "a", "t", "completed", "1", "1", ".", 0, {"goal": "a"})
    second = SubagentSession("subagent_abc222", "dry_run", "plan", "a", "t", "completed", "2", "2", ".", 0, {"goal": "b"})
    write_session(sessions, first)
    write_session(sessions, second)
    assert read_session(sessions, "subagent_abc111").id == "subagent_abc111"
    try:
        read_session(sessions, "subagent_abc")
    except ValueError as exc:
        assert "ambiguous" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected ambiguous prefix failure")


def test_session_from_dict_ignores_unknown_keys() -> None:
    session = SubagentSession.from_dict(
        {
            "id": "subagent_x",
            "backend": "dry_run",
            "mode": "plan",
            "parent_agent_id": "a",
            "task_id": "t",
            "status": "completed",
            "created_at": "1",
            "updated_at": "1",
            "cwd": ".",
            "depth": 0,
            "work_order": {"goal": "x"},
            "future_field": "ignored",
        }
    )
    assert session.id == "subagent_x"


def test_truncate_summary_marks_truncation() -> None:
    summary, truncated = _truncate_summary("x" * 4100)
    assert truncated is True
    assert summary.endswith("... [truncated]")

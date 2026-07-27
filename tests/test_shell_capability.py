"""shell.run: safe-process wiring, policy enforcement, argv-only semantics.
Commands executed here are harmless echoes inside tmp dirs."""

from __future__ import annotations

from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.runtime_context import RuntimeContext
from jigga.runtime.shell import shell_handler
from jigga.tools.safe_process import ProcessPolicyError

_STEP = WorkflowStep(id="s", action="shell.run")


def _agent(shell_perm, fs_allow: list[str]) -> AgentConfig:
    return AgentConfig(id="op", name="Op", role="operator",
                       permissions={"shell": shell_perm, "filesystem": {"allow": fs_allow}})


def _runtime(paths, agent: AgentConfig) -> RuntimeContext:
    return RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                          sessions_dir=paths.home / "sessions")


def test_allowed_command_runs_and_inlines_output(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = _agent({"mode": "allow"}, [str(paths.home)])
    result = shell_handler(_STEP, None, {"command": ["echo", "hello world"]}, {}, _runtime(paths, agent))
    assert result["status"] == "completed" and result["returncode"] == 0
    assert result["stdout_text"].strip() == "hello world"
    assert Path(result["stdout"]).exists()  # full artifact on disk


def test_string_command_is_shlex_split_never_a_shell(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = _agent({"mode": "allow"}, [str(paths.home)])
    # If this went through a shell, `$HOME` would expand; argv semantics keep it literal.
    result = shell_handler(_STEP, None, {"command": "echo $HOME"}, {}, _runtime(paths, agent))
    assert result["stdout_text"].strip() == "$HOME"


def test_default_deny_without_shell_permission(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = AgentConfig(id="op", name="Op", role="r",
                        permissions={"filesystem": {"allow": [str(paths.home)]}})
    with pytest.raises(ProcessPolicyError, match="denied"):
        shell_handler(_STEP, None, {"command": ["echo", "hi"]}, {}, _runtime(paths, agent))


def test_restricted_allowlist_parks_unlisted_commands(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = _agent({"mode": "restricted", "allow": ["echo *"]}, [str(paths.home)])
    ok = shell_handler(_STEP, None, {"command": ["echo", "hi"]}, {}, _runtime(paths, agent))
    assert ok["status"] == "completed"
    held = shell_handler(_STEP, None, {"command": ["ls"]}, {}, _runtime(paths, agent))
    assert held["status"] == "needs_approval" and "returncode" not in (held or {})


def test_dangerous_pattern_denied_even_in_allow_mode(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = _agent({"mode": "allow"}, [str(paths.home)])
    with pytest.raises(ProcessPolicyError, match="dangerous"):
        shell_handler(_STEP, None, {"command": ["rm", "-rf", "/"]}, {}, _runtime(paths, agent))


def test_input_validation(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path, examples=True)
    agent = _agent({"mode": "allow"}, [str(paths.home)])
    with pytest.raises(ValueError, match="input.command"):
        shell_handler(_STEP, None, {}, {}, _runtime(paths, agent))
    with pytest.raises(ValueError, match="non-empty"):
        shell_handler(_STEP, None, {"command": "   "}, {}, _runtime(paths, agent))


def test_capability_registered_high_risk() -> None:
    registry = CapabilityRegistry.load()
    capability = registry.resolve_action("shell.run")
    assert capability is not None and capability.name == "shell"
    assert capability.risk_level == "high"

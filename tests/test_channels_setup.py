from __future__ import annotations

from pathlib import Path

from jigga.cli import _channels_setup
from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml
from jigga.runtime.capabilities import CapabilityRegistry


def _scripted(answers: list[str]):
    it = iter(answers)
    return lambda _p: next(it)


def test_channels_setup_telegram_end_to_end(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    # pick telegram(1) → token → decline discovery → chat ids → default agent → activation: mention(2)
    answers = ["1", "123456789:AAEdummytokendummytokendummytoken00", "n", "111", "daily_briefing_agent", "2"]
    _channels_setup(paths, prompt=_scripted(answers), echo=lambda _m: None)

    # capability installed + approved → its action resolves
    reg = CapabilityRegistry.load(user_capabilities=paths.capabilities, approvals_dir=paths.policies)
    assert reg.resolve_action("telegram.send_message") is not None

    # config written: enabled + allowlist + default_agent (from install) + activation (from wizard)
    tg = read_yaml(paths.config)["channels"]["telegram"]
    assert tg["enabled"] is True
    assert tg["allowed_chat_ids"] == ["111"]
    assert tg["default_agent"] == "daily_briefing_agent"
    assert tg["activation"] == "mention"


def test_channels_setup_invalid_choice_aborts(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    msgs: list[str] = []
    _channels_setup(paths, prompt=_scripted(["nope"]), echo=msgs.append)
    assert any("No channel selected" in m for m in msgs)
    assert "channels" not in read_yaml(paths.config)  # nothing written


def test_channels_setup_grants_channel_tools_to_routed_agent(tmp_path: Path) -> None:
    """Enabling a channel grants the routed agent the channel's full tool set so
    it can actually reply — the round-trip the user otherwise loses silently."""
    from jigga.core.config import load_agents
    from jigga.core.io import write_yaml

    paths = init_runtime(tmp_path)
    write_yaml(paths.agents / "assistant.yaml",
               {"id": "assistant", "name": "Assistant", "role": "pa", "default": True,
                "permission_mode": "autonomous", "tools": ["filesystem.read"]})
    # channel "1", token, discover "n", chat ids, route to "assistant", activation "2"
    answers = ["1", "123456789:AAEdummytokendummytokendummytoken00", "n", "111", "assistant", "2"]
    _channels_setup(paths, prompt=_scripted(answers), echo=lambda *_a, **_k: None)

    agent = load_agents(paths.agents)["assistant"]
    assert "telegram.send_message" in agent.tools       # can reply
    assert "telegram.poll_messages" not in agent.tools  # ingest is runtime-only (supervisor's job)
    assert "filesystem.read" in agent.tools         # existing tools preserved
    # network egress to the channel host is also granted (targeted, not blanket)
    net = agent.permissions["network"]
    assert net["mode"] != "allow"                       # NOT opened to all egress
    assert "https://api.telegram.org" in net["allow"]   # just the channel host


def test_grant_excludes_runtime_only_poll(tmp_path: Path) -> None:
    """The routed agent gets send/reply tools, NEVER the ingest poll — Telegram
    allows one getUpdates consumer; an agent polling collides with the
    supervisor's long-poll or steals the update offset (dropped messages)."""
    paths = init_runtime(tmp_path, examples=True)
    answers = ["1", "123456789:AAEdummytokendummytokendummytoken00", "n", "111",
               "daily_briefing_agent", "1"]
    _channels_setup(paths, prompt=_scripted(answers), echo=lambda _m: None)

    tools = read_yaml(paths.agents / "daily_briefing_agent.yaml")["tools"]
    assert "telegram.send_message" in tools
    assert "telegram.poll_messages" not in tools


def test_runtime_only_action_denied_when_agent_invokes(tmp_path: Path) -> None:
    """Defense in depth for installs that granted poll before the distinction
    existed (the #78 grant-all): an AGENT invoking a runtime-only action is
    denied at dispatch — only the supervisor's ingest path may poll."""
    import json

    import pytest as _pytest

    from jigga.commands.install import install_capability
    from jigga.core.models import AgentConfig, WorkflowStep
    from jigga.runtime.dispatcher import dispatch_action
    from jigga.runtime.runtime_context import RuntimeContext

    paths = init_runtime(tmp_path)
    install_capability(paths, "telegram",
                       input_fn=_scripted(["123456789:AAEdummytokendummytokendummytoken00",
                                           "n", "111", "assistant"]),
                       print_fn=lambda *a, **k: None)
    registry = CapabilityRegistry.load(user_capabilities=paths.capabilities,
                                       approvals_dir=paths.policies)
    agent = AgentConfig(id="assistant", name="A", role="r",
                        tools=["telegram.poll_messages"])      # legacy grant still present
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")

    with _pytest.raises(ValueError, match="runtime-only"):
        dispatch_action(WorkflowStep(id="s", action="telegram.poll_messages"), {}, {},
                        runtime, registry, paths.logs, run_id="r1")

    events = [json.loads(line) for line in (paths.logs / "events.jsonl").read_text().splitlines()]
    denied = [e for e in events if e["type"] == "capability.invocation.denied"]
    assert denied and denied[-1]["details"]["action"] == "telegram.poll_messages"


def test_setup_overwrite_preserves_channel_grants(tmp_path: Path, monkeypatch) -> None:
    """Regression (bit RJ live): `jigga setup --overwrite` regenerates the
    default agent yaml from bundled capabilities only, silently stripping the
    send tool + network egress an enabled channel had granted — renaming the
    assistant broke Telegram replies. Setup must re-apply enabled channels'
    grants after the rewrite."""
    from jigga.cli import main

    paths = init_runtime(tmp_path)
    # Channel install + enable, routed to the (about-to-exist) default agent.
    answers = ["1", "123456789:AAEdummytokendummytokendummytoken00", "n", "111", "chief", "1"]
    _channels_setup(paths, prompt=_scripted(answers), echo=lambda *_a, **_k: None)
    # Create the default agent via setup, then rename it with --overwrite
    # (the exact sequence that lost the grant).
    setup_answers = iter(["RJ", "", "", "1", "Chief", "1", ""])
    monkeypatch.setattr("builtins.input", lambda _p="": next(setup_answers, ""))
    assert main(["--home", str(tmp_path), "setup", "--overwrite"]) == 0

    doc = read_yaml(paths.agents / "chief.yaml")
    assert doc["name"] == "Chief"                                  # the overwrite happened
    assert "telegram.send_message" in doc["tools"]                 # grant restored
    assert "telegram.poll_messages" not in doc["tools"]            # runtime-only stays out
    assert "https://api.telegram.org" in doc["permissions"]["network"]["allow"]

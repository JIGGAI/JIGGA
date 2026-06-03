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
    assert "telegram.send_message" in agent.tools   # can reply
    assert "telegram.poll_messages" in agent.tools  # full action set granted
    assert "filesystem.read" in agent.tools         # existing tools preserved
    # network egress to the channel host is also granted (targeted, not blanket)
    net = agent.permissions["network"]
    assert net["mode"] != "allow"                       # NOT opened to all egress
    assert "https://api.telegram.org" in net["allow"]   # just the channel host

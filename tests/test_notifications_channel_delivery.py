"""notifications.send → user's default channel.

The batteries-included contract: an example agent recipe declares
`notifications.send` (+ `notifications: {channel: default}`) and the runtime
delivers to whatever channel the user actually connected — via the channel's
own credentials, never per-agent telegram tools/network grants — so installing
a channel is the ONLY action a user takes to get briefings on their phone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.channels import (
    ensure_default_channel,
    owner_conversation,
    resolve_default_channel,
)
from jigga.runtime.handlers import _notifications_handler
from jigga.runtime.notifications import NotificationResult
from jigga.runtime.runtime_context import RuntimeContext


@pytest.fixture(autouse=True)
def _no_real_desktop(monkeypatch):
    """Tests never touch the real system: desktop delivery is always stubbed,
    even in 'real' mode (conftest's dry_run env is flipped per-test below)."""
    sent: list = []

    def _stub(request, *, dry_run=False):
        sent.append((request, dry_run))
        return NotificationResult(delivered=False, backend="stub")

    monkeypatch.setattr("jigga.runtime.handlers.send_notification", _stub)
    return sent


def _enable_telegram(paths, *, allowed=("111", "222"), default_key=True, **extra) -> None:
    config = read_yaml(paths.config)
    channels = config.setdefault("channels", {})
    channels["telegram"] = {"enabled": True, "allowed_chat_ids": list(allowed), **extra}
    if default_key:
        channels["default"] = "telegram"
    write_yaml(paths.config, config)


def _agent(channel: str | None = "default") -> AgentConfig:
    notifications = {} if channel is None else {"channel": channel}
    return AgentConfig(id="daily_briefing_agent", name="Briefing", role="briefs",
                       notifications=notifications)


def _notify(paths, *, agent=None, input=None):
    step = WorkflowStep(id="notify", action="notifications.send")
    runtime = RuntimeContext(agent=agent if agent is not None else _agent(),
                             home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    payload = input if input is not None else {"title": "Morning briefing", "body": "All clear."}
    return _notifications_handler(step, None, payload, {}, runtime)


def _events(paths) -> list[dict]:
    path = paths.logs / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


# --- config resolution -------------------------------------------------------


def test_ensure_default_channel_sets_once() -> None:
    config: dict = {"channels": {"telegram": {"enabled": True}}}
    ensure_default_channel(config, "telegram")
    assert config["channels"]["default"] == "telegram"
    ensure_default_channel(config, "slack")  # a later channel must NOT steal the default
    assert config["channels"]["default"] == "telegram"


def test_resolve_default_channel_explicit_then_fallback(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    assert resolve_default_channel(paths.home) is None  # nothing connected
    _enable_telegram(paths, default_key=False)
    # Pre-default-key config (older installs): first enabled channel wins.
    assert resolve_default_channel(paths.home) == "telegram"
    _enable_telegram(paths, default_key=True)
    assert resolve_default_channel(paths.home) == "telegram"


def test_resolve_default_channel_ignores_disabled_or_unregistered(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    config = read_yaml(paths.config)
    config["channels"] = {"default": "slack",                      # no registered adapter
                          "slack": {"enabled": True},
                          "telegram": {"enabled": False}}          # disabled
    write_yaml(paths.config, config)
    assert resolve_default_channel(paths.home) is None


def test_owner_conversation_prefers_notify_chat_id(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _enable_telegram(paths, allowed=("111", "222"), notify_chat_id="999")
    assert owner_conversation(paths.home) == ("telegram", "999")


def test_owner_conversation_falls_back_to_first_allowlisted(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _enable_telegram(paths, allowed=("111", "222"))
    assert owner_conversation(paths.home) == ("telegram", "111")


def test_owner_conversation_none_without_ids(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _enable_telegram(paths, allowed=())
    assert owner_conversation(paths.home) is None


# --- handler delivery --------------------------------------------------------


def test_notification_delivers_to_default_channel(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    _enable_telegram(paths)
    monkeypatch.setenv("JIGGA_NOTIFICATION_MODE", "real")
    calls: list = []
    monkeypatch.setattr("jigga.runtime.telegram.send_message",
                        lambda home, chat_id, text: calls.append((chat_id, text)) or {"status": "ok"})

    output = _notify(paths)

    assert calls == [("111", "Morning briefing\n\nAll clear.")]
    assert output["delivered"] is True               # channel counts even though desktop stub failed
    assert output["channel"] == {"channel": "telegram", "delivered": True}
    types = [e["type"] for e in _events(paths)]
    assert "notification.channel_delivered" in types
    assert types[-1] == "notification.delivered"


def test_notification_destination_comes_from_config_not_model(tmp_path: Path, monkeypatch) -> None:
    """An agent/model-supplied chat_id must be ignored — delivery can only go
    to the config-resolved owner conversation (no redirect/exfil channel)."""
    paths = init_runtime(tmp_path)
    _enable_telegram(paths)
    monkeypatch.setenv("JIGGA_NOTIFICATION_MODE", "real")
    calls: list = []
    monkeypatch.setattr("jigga.runtime.telegram.send_message",
                        lambda home, chat_id, text: calls.append(chat_id) or {"status": "ok"})

    _notify(paths, input={"title": "t", "body": "b", "chat_id": "666", "conversation_id": "666"})

    assert calls == ["111"]


def test_dry_run_skips_channel_send(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)  # conftest forces dry_run mode
    _enable_telegram(paths)
    sentinel = lambda *a, **k: pytest.fail("must not hit telegram in dry_run")  # noqa: E731
    monkeypatch.setattr("jigga.runtime.telegram.send_message", sentinel)

    output = _notify(paths)

    assert output["delivered"] is False
    assert output["channel"] == {"channel": "telegram", "delivered": False, "dry_run": True}


def test_desktop_preference_opts_out_of_channel(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    _enable_telegram(paths)
    monkeypatch.setenv("JIGGA_NOTIFICATION_MODE", "real")
    sentinel = lambda *a, **k: pytest.fail("desktop preference must not hit the channel")  # noqa: E731
    monkeypatch.setattr("jigga.runtime.telegram.send_message", sentinel)

    output = _notify(paths, agent=_agent("desktop"))

    assert output["channel"] is None


def test_channel_failure_is_contained(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    _enable_telegram(paths)
    monkeypatch.setenv("JIGGA_NOTIFICATION_MODE", "real")

    def _boom(home, chat_id, text):
        raise RuntimeError("bot token revoked")

    monkeypatch.setattr("jigga.runtime.telegram.send_message", _boom)

    output = _notify(paths)  # must NOT raise

    assert output["delivered"] is False
    assert output["channel"]["error"] == "bot token revoked"
    failures = [e for e in _events(paths) if e["type"] == "notification.channel_failed"]
    assert failures and failures[-1]["details"]["channel"] == "telegram"
    assert [e["type"] for e in _events(paths)][-1] == "notification.failed"


def test_no_channel_connected_is_desktop_only(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    monkeypatch.setenv("JIGGA_NOTIFICATION_MODE", "real")
    output = _notify(paths)
    assert output["channel"] is None
    assert output["backend"] == "stub"


# --- batteries-included recipe guard -----------------------------------------


def test_briefing_recipe_is_self_contained() -> None:
    """The bundled example must define everything the agent needs: a delivery
    channel preference and a cron message that instructs actual delivery —
    a scheduled wake with no instruction is a recipe bug."""
    recipe = read_yaml(Path(__file__).resolve().parents[1] / "examples" / "agents"
                       / "daily_briefing_agent.yaml")
    assert recipe["notifications"]["channel"] == "default"
    schedule = recipe["wake"]["schedules"][0]
    assert "notifications.send" in schedule["message"]
    assert "notifications.send" in recipe["tools"]


def test_agent_yaml_notifications_field_roundtrips() -> None:
    agent = AgentConfig.from_dict({"id": "a", "name": "A", "role": "r",
                                   "notifications": {"channel": "telegram"}})
    assert agent.notifications == {"channel": "telegram"}


def test_telegram_setup_sets_default_channel(tmp_path: Path) -> None:
    from jigga.optional_capabilities.telegram import _write_telegram_config

    paths = init_runtime(tmp_path)
    _write_telegram_config(paths.config, allowed_chat_ids=["111"], default_agent="assistant")
    config = read_yaml(paths.config)
    assert config["channels"]["default"] == "telegram"

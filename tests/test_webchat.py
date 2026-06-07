"""Webchat channel (M2) — the browser as a JIGGA channel.

File-backed: `jigga webchat send` appends inbox.jsonl, the adapter polls it
past a stored offset into the NORMAL channel pipeline, the agent replies via
the `webchat.send_message` tool into outbox.jsonl, and `--wait` makes the
round trip synchronous for the jiggaview Chat page."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from jigga.cli import _channels_setup, main
from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime import webchat
from jigga.runtime.channel_listener import (
    ingest_once,
    long_polling_channels_enabled,
)
from jigga.runtime.tasks import list_tasks


def _write_default_agent(paths, agent_id="assistant", tools=()) -> None:
    write_yaml(paths.agents / f"{agent_id}.yaml",
               {"id": agent_id, "name": "Assistant", "role": "pa", "default": True,
                "permission_mode": "autonomous", "tools": list(tools)})


def _enable_webchat(paths) -> None:
    config = read_yaml(paths.config)
    config.setdefault("channels", {})["webchat"] = {"enabled": True}
    write_yaml(paths.config, config)


# --- module: inbox / poll / offset ------------------------------------------


def test_append_and_poll_roundtrip_advances_offset(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    entry = webchat.append_inbound(paths.home, "hello", sender="rj")
    assert entry["id"].startswith("wcm")

    result = webchat.poll_messages(paths.home)
    assert result["status"] == "ok"
    [msg] = result["messages"]
    assert msg["text"] == "hello"
    assert msg["sender"] == "rj"
    assert msg["chat_id"] == "web"
    assert msg["chat_type"] == "private"
    assert msg["message_id"] == entry["id"]

    # offset consumed — a second poll sees nothing (no double-processing)
    assert webchat.poll_messages(paths.home)["messages"] == []
    # new message after the offset is picked up
    webchat.append_inbound(paths.home, "again")
    assert [m["text"] for m in webchat.poll_messages(paths.home)["messages"]] == ["again"]


def test_poll_respects_limit_and_resumes(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    for i in range(5):
        webchat.append_inbound(paths.home, f"m{i}")
    first = webchat.poll_messages(paths.home, limit=2)
    assert [m["text"] for m in first["messages"]] == ["m0", "m1"]
    rest = webchat.poll_messages(paths.home, limit=50)
    assert [m["text"] for m in rest["messages"]] == ["m2", "m3", "m4"]


def test_corrupt_offset_reprocesses_from_zero(tmp_path: Path) -> None:
    """A corrupt offset must never lose messages — worst case is reprocessing."""
    paths = init_runtime(tmp_path)
    webchat.append_inbound(paths.home, "hi")
    assert len(webchat.poll_messages(paths.home)["messages"]) == 1
    (paths.home / "state" / "webchat_offset.json").write_text("{not json", encoding="utf-8")
    assert webchat.load_offset(paths.home) == 0
    assert [m["text"] for m in webchat.poll_messages(paths.home)["messages"]] == ["hi"]


def test_corrupt_jsonl_lines_skipped(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    webchat.append_inbound(paths.home, "good")
    inbox = paths.home / "channels" / "webchat" / "inbox.jsonl"
    with inbox.open("a", encoding="utf-8") as fh:
        fh.write("{broken\n")
        fh.write('"not a dict"\n')
    webchat.append_inbound(paths.home, "also good")
    assert [m["text"] for m in webchat.poll_messages(paths.home)["messages"]] == ["good", "also good"]


# --- module: outbox / history -------------------------------------------------


def test_send_message_appends_outbox(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    result = webchat.send_message(paths.home, "web", "reply text")
    assert result["status"] == "ok"
    assert result["message_id"].startswith("wcr")
    outbox = paths.home / "channels" / "webchat" / "outbox.jsonl"
    [entry] = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines()]
    assert entry["sender"] == "agent"
    assert entry["text"] == "reply text"
    assert entry["conversation_id"] == "web"


def test_history_merges_chronologically_and_filters_conversation(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    webchat.append_inbound(paths.home, "q1")
    webchat.send_message(paths.home, "web", "a1")
    webchat.append_inbound(paths.home, "other room", conversation_id="room2")
    merged = webchat.history(paths.home)
    assert [(e["sender"], e["text"]) for e in merged] == [("you", "q1"), ("agent", "a1")]
    assert [e["text"] for e in webchat.history(paths.home, conversation_id="room2")] == ["other room"]


def test_history_interleaves_by_timestamp_not_file_order(tmp_path: Path) -> None:
    """A reply between two questions must sort between them — the naive
    inbox-then-outbox concatenation order is wrong for any real conversation."""
    paths = init_runtime(tmp_path)
    channel_dir = paths.home / "channels" / "webchat"
    channel_dir.mkdir(parents=True)
    rows = [
        ("inbox.jsonl", "you", "q1", "2026-06-06T10:00:00Z"),
        ("inbox.jsonl", "you", "q2", "2026-06-06T10:02:00Z"),
        ("outbox.jsonl", "agent", "a1", "2026-06-06T10:01:00Z"),
        ("outbox.jsonl", "agent", "a2", "2026-06-06T10:03:00Z"),
    ]
    for name, sender, text, ts in rows:
        with (channel_dir / name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": text, "conversation_id": "web", "sender": sender,
                                 "text": text, "ts": ts}) + "\n")
    assert [e["text"] for e in webchat.history(paths.home)] == ["q1", "a1", "q2", "a2"]


def test_history_limit_keeps_most_recent(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    for i in range(5):
        webchat.append_inbound(paths.home, f"m{i}")
    assert [e["text"] for e in webchat.history(paths.home, limit=2)] == ["m3", "m4"]


# --- adapter -------------------------------------------------------------------


def test_adapter_poll_produces_webchat_events(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    webchat.append_inbound(paths.home, "hello agent", sender="rj")
    adapter = webchat.WebchatAdapter()
    result = adapter.poll(paths.home)
    assert result["status"] == "ok"
    [event] = result["events"]
    assert event.source == "webchat"
    assert event.text == "hello agent"
    assert event.actor_name == "rj"
    assert event.is_direct  # private conversation → activation modes behave like a DM


def test_adapter_send_writes_outbox(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    webchat.WebchatAdapter().send(paths.home, conversation_id="web", text="out")
    assert [e["text"] for e in webchat.history(paths.home)] == ["out"]


def test_long_polls_flags_pace_the_supervisor_loop(tmp_path: Path) -> None:
    """Webchat reads a local file and returns instantly — it must NOT claim
    long-polling or the daemon loop would drop its inter-tick sleep and spin."""
    from jigga.runtime.channels import TelegramAdapter

    assert TelegramAdapter.long_polls is True
    assert webchat.WebchatAdapter.long_polls is False

    paths = init_runtime(tmp_path)
    assert not long_polling_channels_enabled(paths.home)
    _enable_webchat(paths)
    assert not long_polling_channels_enabled(paths.home)   # webchat alone: cron cadence
    config = read_yaml(paths.config)
    config["channels"]["telegram"] = {"enabled": True}
    write_yaml(paths.config, config)
    assert long_polling_channels_enabled(paths.home)       # telegram blocks → back-to-back ticks


# --- capability handler / dispatch ----------------------------------------------


def _dispatch(paths, agent_tools, action, payload):
    from jigga.core.models import AgentConfig, WorkflowStep
    from jigga.runtime.capabilities import CapabilityRegistry
    from jigga.runtime.dispatcher import dispatch_action
    from jigga.runtime.runtime_context import RuntimeContext

    registry = CapabilityRegistry.load()
    agent = AgentConfig(id="assistant", name="A", role="r", tools=list(agent_tools))
    runtime = RuntimeContext(agent=agent, home=paths.home, logs_dir=paths.logs,
                             sessions_dir=paths.home / "sessions")
    return dispatch_action(WorkflowStep(id="s", action=action), payload, {},
                           runtime, registry, paths.logs, run_id="r1")


def test_agent_replies_via_send_message_tool(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    result = _dispatch(paths, ["webchat.send_message"], "webchat.send_message",
                       {"text": "tool reply", "chat_id": "web"})
    assert result["status"] == "ok"
    assert [e["text"] for e in webchat.history(paths.home)] == ["tool reply"]


def test_send_message_requires_text(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    with pytest.raises(ValueError, match="requires 'text'"):
        _dispatch(paths, ["webchat.send_message"], "webchat.send_message", {"chat_id": "web"})


def test_poll_is_runtime_only_for_agents(tmp_path: Path) -> None:
    """Ingest belongs to the pipeline: an agent invoking webchat.poll_messages
    is denied at dispatch even if a legacy grant listed it."""
    paths = init_runtime(tmp_path)
    with pytest.raises(ValueError, match="runtime-only"):
        _dispatch(paths, ["webchat.poll_messages"], "webchat.poll_messages", {})


def test_handler_rejects_unknown_action(tmp_path: Path) -> None:
    from jigga.core.models import WorkflowStep

    paths = init_runtime(tmp_path)

    class _Runtime:
        home = paths.home

    with pytest.raises(ValueError, match="Unknown webchat action"):
        webchat.webchat_handler(WorkflowStep(id="s", action="webchat.bogus"), None, {}, {}, _Runtime())


def test_onboard_tool_grant_excludes_runtime_only_actions() -> None:
    from jigga.commands.onboard import _all_capability_actions

    actions = _all_capability_actions()
    assert "webchat.send_message" in actions
    assert "webchat.poll_messages" not in actions


# --- ingest: only_channel scoping ------------------------------------------------


def test_ingest_only_channel_skips_other_enabled_channels(tmp_path: Path) -> None:
    """`webchat send --wait` must never block behind telegram's long-poll."""
    paths = init_runtime(tmp_path)
    _write_default_agent(paths)
    config = read_yaml(paths.config)
    config["channels"] = {
        "telegram": {"enabled": True, "allowed_chat_ids": ["111"], "default_agent": "assistant"},
        "webchat": {"enabled": True},
    }
    write_yaml(paths.config, config)
    webchat.append_inbound(paths.home, "scoped")

    def _explode(*_a, **_k):
        raise AssertionError("telegram polled during webchat-scoped ingest")

    with patch("jigga.runtime.telegram.poll_messages", _explode), \
         patch("jigga.runtime.channel_listener.run_agent", lambda *a, **k: {"ok": True}):
        summary = ingest_once(paths.home, paths.logs, paths.tasks, paths.agents,
                              long_poll_seconds=0, only_channel="webchat")

    assert summary["polled"] == ["webchat"]
    [task] = list_tasks(paths.tasks)
    assert task.metadata["channel"] == "webchat"
    assert task.assignee == "assistant"


# --- multi-agent: --agent targeting ------------------------------------------------


def test_send_with_agent_routes_to_that_agent(tmp_path: Path) -> None:
    """The chat page's agent picker: --agent addresses a specific agent — its
    own thread (conversation = agent id), the task assigned to it, the run on
    it, and the reply tool granted to it."""
    paths = init_runtime(tmp_path)
    _write_default_agent(paths)                       # assistant = channel default
    write_yaml(paths.agents / "specialist.yaml",
               {"id": "specialist", "name": "Spec", "role": "specialist",
                "permission_mode": "autonomous", "tools": []})
    ran: list[str] = []

    def fake_run_agent(home, logs, tasks, agents, agent_id, **kw):
        ran.append(agent_id)
        webchat.send_message(home, "specialist", "specialist here")
        return {"agent_id": agent_id}

    with patch("jigga.runtime.channel_listener.run_agent", fake_run_agent):
        assert main(["--home", str(tmp_path), "webchat", "send", "--wait",
                     "--agent", "specialist", "--text", "ping the specialist"]) == 0

    assert ran == ["specialist"]                       # not the default agent
    [task] = list_tasks(paths.tasks)
    assert task.assignee == "specialist"
    assert task.metadata["chat_id"] == "specialist"    # thread = agent id
    # reply tool granted to the targeted agent, not just the default
    assert "webchat.send_message" in read_yaml(paths.agents / "specialist.yaml")["tools"]
    # threads are separate: the specialist's thread has the exchange, web is empty
    assert [e["sender"] for e in webchat.history(paths.home, conversation_id="specialist")] == \
        ["you", "agent"]
    assert webchat.history(paths.home) == []


def test_unknown_target_agent_falls_back_to_default(tmp_path: Path) -> None:
    """A stale/typo'd target (e.g. an agent deleted after the message was
    written) must not drop the message — audit + route to the default."""
    import json as _json

    paths = init_runtime(tmp_path)
    _write_default_agent(paths)
    _enable_webchat(paths)
    webchat.append_inbound(paths.home, "anyone home?", target_agent="ghost")

    with patch("jigga.runtime.channel_listener.run_agent", lambda *a, **k: {"ok": True}):
        ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0)

    [task] = list_tasks(paths.tasks)
    assert task.assignee == "assistant"
    events = [_json.loads(line) for line
              in (paths.logs / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    unknown = [e for e in events if e["type"] == "channel.target_unknown"]
    assert unknown and unknown[0]["details"]["requested_agent"] == "ghost"


def test_cli_send_rejects_unknown_agent(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _write_default_agent(paths)
    assert main(["--home", str(tmp_path), "webchat", "send",
                 "--agent", "ghost", "--text", "x"]) == 1
    assert webchat.history(paths.home, conversation_id="ghost") == []   # nothing appended


def test_explicit_conversation_overrides_agent_thread(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _write_default_agent(paths)
    assert main(["--home", str(tmp_path), "webchat", "send", "--agent", "assistant",
                 "--conversation", "room42", "--text", "hi"]) == 0
    assert [e["text"] for e in webchat.history(paths.home, conversation_id="room42")] == ["hi"]


# --- CLI: send / --wait / history -------------------------------------------------


def test_cli_send_auto_enables_channel_and_grants_reply_tool(tmp_path: Path) -> None:
    """Typing in the local chat IS the opt-in: first send enables the channel,
    claims the default-channel slot, and grants the routed agent the reply tool."""
    paths = init_runtime(tmp_path)
    _write_default_agent(paths, tools=["filesystem.read_file"])

    assert main(["--home", str(tmp_path), "webchat", "send", "--text", "hi there"]) == 0

    config = read_yaml(paths.config)
    assert config["channels"]["webchat"]["enabled"] is True
    assert config["channels"]["default"] == "webchat"
    tools = read_yaml(paths.agents / "assistant.yaml")["tools"]
    assert "webchat.send_message" in tools
    assert "webchat.poll_messages" not in tools             # ingest stays runtime-only
    assert "filesystem.read_file" in tools                  # existing grants preserved
    # message landed in the inbox, unconsumed (supervisor backstop will ingest)
    assert [e["text"] for e in webchat.history(paths.home)] == ["hi there"]
    assert webchat.load_offset(paths.home) == 0


def test_cli_send_does_not_steal_existing_default_channel(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    config = read_yaml(paths.config)
    config["channels"] = {"default": "telegram", "telegram": {"enabled": True}}
    write_yaml(paths.config, config)

    assert main(["--home", str(tmp_path), "webchat", "send", "--text", "x"]) == 0
    assert read_yaml(paths.config)["channels"]["default"] == "telegram"


def test_cli_send_wait_returns_agent_reply(tmp_path: Path, capsys) -> None:
    """The synchronous chat round trip: send --wait ingests webchat-only inline
    and returns exactly the replies this message produced."""
    paths = init_runtime(tmp_path)
    _write_default_agent(paths)
    # a stale agent reply from an earlier exchange must NOT be re-reported
    webchat.send_message(paths.home, "web", "old reply")

    def fake_run_agent(home, logs, tasks, agents, agent_id, **kw):
        webchat.send_message(home, "web", "fresh reply")
        return {"agent_id": agent_id}

    with patch("jigga.runtime.channel_listener.run_agent", fake_run_agent):
        assert main(["--home", str(tmp_path), "webchat", "send", "--wait", "--json",
                     "--text", "question?"]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["message"]["text"] == "question?"
    assert [r["text"] for r in out["replies"]] == ["fresh reply"]
    assert out["channel_enabled_now"] is True


def test_cli_send_wait_consumes_offset_no_double_processing(tmp_path: Path) -> None:
    """The supervisor's backstop poll must not re-run a message --wait handled."""
    paths = init_runtime(tmp_path)
    _write_default_agent(paths)
    runs: list[str] = []

    def fake_run_agent(home, logs, tasks, agents, agent_id, **kw):
        runs.append(agent_id)
        return {"agent_id": agent_id}

    with patch("jigga.runtime.channel_listener.run_agent", fake_run_agent):
        assert main(["--home", str(tmp_path), "webchat", "send", "--wait",
                     "--text", "once only"]) == 0
        assert len(list_tasks(paths.tasks)) == 1
        # the backstop cycle: nothing new to ingest
        summary = ingest_once(paths.home, paths.logs, paths.tasks, paths.agents,
                              long_poll_seconds=0)
        assert summary["created"] == []
        assert len(list_tasks(paths.tasks)) == 1


def test_cli_history_json(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    webchat.append_inbound(paths.home, "q")
    webchat.send_message(paths.home, "web", "a")
    assert main(["--home", str(tmp_path), "webchat", "history", "--json"]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert [(e["sender"], e["text"]) for e in entries] == [("you", "q"), ("agent", "a")]


def test_cli_history_respects_conversation_and_limit(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    for i in range(3):
        webchat.append_inbound(paths.home, f"m{i}", conversation_id="room")
    webchat.append_inbound(paths.home, "elsewhere")
    assert main(["--home", str(tmp_path), "webchat", "history", "--json",
                 "--conversation", "room", "--limit", "2"]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert [e["text"] for e in entries] == ["m1", "m2"]


# --- channels setup (wizard) -------------------------------------------------------


def test_channels_setup_webchat_skips_install(tmp_path: Path) -> None:
    """Webchat is bundled — the wizard enables it without any capability
    install/auth step (catalog capability=None)."""
    paths = init_runtime(tmp_path)

    def _scripted(answers):
        it = iter(answers)
        return lambda _p: next(it)

    # pick webchat (sorted: telegram=1, webchat=2) → activation: always(1)
    _channels_setup(paths, prompt=_scripted(["2", "1"]), echo=lambda *_a, **_k: None)
    cfg = read_yaml(paths.config)["channels"]
    assert cfg["webchat"]["enabled"] is True
    assert cfg["webchat"]["activation"] == "always"
    assert cfg["default"] == "webchat"


# --- thread-context injection (the model is stateless; JIGGA is the chat client) --


def test_thread_context_renders_recent_turns_oldest_first(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    m1 = webchat.append_inbound(paths.home, "what models do we support?")
    webchat.send_message(paths.home, "web", "ChatGPT today; Claude next.")
    m3 = webchat.append_inbound(paths.home, "what about the second option?")

    rendered = webchat.thread_context(paths.home, "web", exclude_message_id=m3["id"])
    assert rendered == "you: what models do we support?\nagent: ChatGPT today; Claude next."
    assert m1["id"]  # (no exclusion of earlier messages)


def test_thread_context_respects_config_turns_and_zero_disables(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    for i in range(6):
        webchat.append_inbound(paths.home, f"m{i}")
    config = read_yaml(paths.config)
    config.setdefault("channels", {})["webchat"] = {"enabled": True, "context_turns": 2}
    write_yaml(paths.config, config)
    assert webchat.thread_context(paths.home, "web") == "you: m4\nyou: m5"

    config["channels"]["webchat"]["context_turns"] = 0
    write_yaml(paths.config, config)
    assert webchat.thread_context(paths.home, "web") == ""


def test_thread_context_char_cap_drops_oldest(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    webchat.append_inbound(paths.home, "OLDEST " + "x" * webchat.CONTEXT_CHAR_CAP)
    webchat.append_inbound(paths.home, "newest matters")
    rendered = webchat.thread_context(paths.home, "web")
    assert rendered == "you: newest matters"      # oldest overflow dropped at a line boundary
    assert len(rendered) <= webchat.CONTEXT_CHAR_CAP


def test_thread_context_empty_thread_is_empty(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    assert webchat.thread_context(paths.home, "web") == ""


def test_agent_prompt_carries_thread_history(tmp_path: Path) -> None:
    """End-to-end: a webchat follow-up runs with the conversation's tail in
    the user message — and the current message is NOT duplicated (it's the
    task body)."""
    from jigga.runtime.model_router import ModelCallResult

    paths = init_runtime(tmp_path)
    _write_default_agent(paths)
    _enable_webchat(paths)
    # An earlier, already-handled exchange in the thread (consume the offset
    # so only the follow-up will be pending at ingest):
    webchat.append_inbound(paths.home, "list our channels")
    assert len(webchat.poll_messages(paths.home)["messages"]) == 1
    webchat.send_message(paths.home, "web", "telegram and webchat")
    requests = []

    def capture_call_model(home, logs, request):
        requests.append(request)
        return ModelCallResult(status="ok", content="the second one is webchat",
                               model="fake", provider="fake", dry_run=True)

    with patch("jigga.runtime.agent.call_model", capture_call_model):
        assert main(["--home", str(tmp_path), "webchat", "send", "--wait",
                     "--text", "tell me more about the second one"]) == 0

    # find the task-run request (ingest consumed BOTH pending inbox messages →
    # two tasks; the follow-up task is the one whose body has the new text)
    user_items = [i for r in requests for i in r.items if i.role == "user"
                  and "tell me more about the second one" in i.content]
    assert user_items, "follow-up task request not captured"
    content = user_items[0].content
    assert "Recent conversation in this thread" in content
    assert "you: list our channels" in content
    assert "agent: telegram and webchat" in content
    # the current message appears once (task body), not again in the history
    assert content.count("tell me more about the second one") == 1


def test_non_channel_tasks_get_no_thread_header(tmp_path: Path) -> None:
    from jigga.runtime.model_router import ModelCallResult
    from jigga.runtime.agent import run_agent
    from jigga.runtime.tasks import create_task

    paths = init_runtime(tmp_path)
    _write_default_agent(paths)
    webchat.append_inbound(paths.home, "unrelated chat noise")   # transcript exists
    create_task(paths.tasks, "Plain scheduled work", assignee="assistant")
    requests = []

    def capture_call_model(home, logs, request):
        requests.append(request)
        return ModelCallResult(status="ok", content="done", model="fake", provider="fake", dry_run=True)

    with patch("jigga.runtime.agent.call_model", capture_call_model):
        run_agent(paths.home, paths.logs, paths.tasks, paths.agents, "assistant")

    contents = [i.content for r in requests for i in r.items if i.role == "user"]
    assert contents and all("Recent conversation" not in c for c in contents)


def test_thread_context_window_is_full_even_with_exclusion(tmp_path: Path) -> None:
    """Excluding the current message must not shrink the window below
    `context_turns` — the renderer over-fetches by one to compensate."""
    paths = init_runtime(tmp_path)
    webchat.append_inbound(paths.home, "first")
    webchat.append_inbound(paths.home, "second")
    current = webchat.append_inbound(paths.home, "the current one")
    config = read_yaml(paths.config)
    config.setdefault("channels", {})["webchat"] = {"enabled": True, "context_turns": 2}
    write_yaml(paths.config, config)
    rendered = webchat.thread_context(paths.home, "web", exclude_message_id=current["id"])
    assert rendered == "you: first\nyou: second"   # both turns, not just one


# --- rolling per-conversation summary (conversational compaction) ----------------


def _summary_result(text="SUMMARY", status="ok"):
    from jigga.runtime.model_router import ModelCallResult
    return ModelCallResult(status=status, content=text, model="fake",
                           provider="fake", dry_run=True)


def _set_turns(paths, turns, **extra) -> None:
    config = read_yaml(paths.config)
    config.setdefault("channels", {})["webchat"] = {"enabled": True,
                                                    "context_turns": turns, **extra}
    write_yaml(paths.config, config)


def test_summary_path_is_traversal_proof(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    path = webchat._summary_path(paths.home, "../../../etc/passwd")
    assert path.parent == paths.home / "channels" / "webchat" / "summaries"
    # distinct ids that sanitize identically still get distinct files (hash suffix)
    assert webchat._summary_path(paths.home, "a/b") != webchat._summary_path(paths.home, "a_b")


def test_roll_summary_noop_under_window(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_turns(paths, 5)
    for i in range(5):
        webchat.append_inbound(paths.home, f"m{i}")

    def explode(*_a, **_k):
        raise AssertionError("model called with no overflow")

    with patch("jigga.runtime.model_router.call_model", explode):
        assert webchat.roll_summary(paths.home, paths.logs, "web") == ""


def test_roll_summary_folds_overflow_and_advances_watermark(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_turns(paths, 2)
    entries = [webchat.append_inbound(paths.home, f"m{i}") for i in range(5)]
    calls = []

    def capture(home, logs, request):
        calls.append(request)
        return _summary_result("the early turns discussed m0-m2")

    with patch("jigga.runtime.model_router.call_model", capture):
        summary = webchat.roll_summary(paths.home, paths.logs, "web")

    assert summary == "the early turns discussed m0-m2"
    prompt = calls[0].items[-1].content
    assert "m0" in prompt and "m2" in prompt     # overflow folded
    assert "m3" not in prompt and "m4" not in prompt   # window turns NOT folded
    record = webchat.load_summary(paths.home, "web")
    assert record["through_message_id"] == entries[2]["id"]

    # nothing new overflowed → stored summary returned, model NOT called again
    def explode(*_a, **_k):
        raise AssertionError("model re-called without new overflow")

    with patch("jigga.runtime.model_router.call_model", explode):
        assert webchat.roll_summary(paths.home, paths.logs, "web") == summary


def test_roll_summary_incremental_fold_includes_existing(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_turns(paths, 2)
    for i in range(5):
        webchat.append_inbound(paths.home, f"m{i}")
    with patch("jigga.runtime.model_router.call_model",
               lambda *a, **k: _summary_result("v1 summary")):
        webchat.roll_summary(paths.home, paths.logs, "web")
    webchat.append_inbound(paths.home, "m5")     # pushes m3 out of the window
    calls = []

    def capture(home, logs, request):
        calls.append(request)
        return _summary_result("v2 summary")

    with patch("jigga.runtime.model_router.call_model", capture):
        assert webchat.roll_summary(paths.home, paths.logs, "web") == "v2 summary"
    prompt = calls[0].items[-1].content
    assert "v1 summary" in prompt                # folds INTO the existing summary
    assert "m3" in prompt and "m2" not in prompt # only the newly-overflowed turn


def test_roll_summary_model_failure_keeps_previous_state(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_turns(paths, 2)
    for i in range(5):
        webchat.append_inbound(paths.home, f"m{i}")
    with patch("jigga.runtime.model_router.call_model",
               lambda *a, **k: _summary_result("good", status="ok")):
        webchat.roll_summary(paths.home, paths.logs, "web")
    before = webchat.load_summary(paths.home, "web")
    webchat.append_inbound(paths.home, "m5")
    with patch("jigga.runtime.model_router.call_model",
               lambda *a, **k: _summary_result("", status="error")):
        assert webchat.roll_summary(paths.home, paths.logs, "web") == "good"
    assert webchat.load_summary(paths.home, "web") == before   # watermark NOT advanced


def test_roll_summary_disabled_by_config(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_turns(paths, 2, summarize=False)
    for i in range(5):
        webchat.append_inbound(paths.home, f"m{i}")

    def explode(*_a, **_k):
        raise AssertionError("summarize=false must not call the model")

    with patch("jigga.runtime.model_router.call_model", explode):
        assert webchat.roll_summary(paths.home, paths.logs, "web") == ""


def test_corrupt_summary_file_is_ignored(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    path = webchat._summary_path(paths.home, "web")
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    assert webchat.load_summary(paths.home, "web") == {}


def test_adapter_block_renders_summary_above_tail(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_turns(paths, 2)
    for i in range(5):
        webchat.append_inbound(paths.home, f"m{i}")
    with patch("jigga.runtime.model_router.call_model",
               lambda *a, **k: _summary_result("they covered m0 through m2")):
        block = webchat.WebchatAdapter().thread_context(
            paths.home, conversation_id="web", logs_dir=paths.logs)
    summary_at = block.index("Earlier in this conversation (summary)")
    tail_at = block.index("Recent conversation in this thread")
    assert summary_at < tail_at
    assert "they covered m0 through m2" in block
    assert "you: m3\nyou: m4" in block


def test_agent_prompt_carries_summary_for_long_threads(tmp_path: Path) -> None:
    """End-to-end: once a thread outgrows the window, the agent's prompt gets
    summary + tail + current task — full continuity at bounded cost."""
    from jigga.runtime.model_router import ModelCallResult

    paths = init_runtime(tmp_path)
    _write_default_agent(paths)
    _set_turns(paths, 2)
    for i in range(4):                            # already-handled earlier turns
        webchat.append_inbound(paths.home, f"m{i}")
    assert len(webchat.poll_messages(paths.home, limit=50)["messages"]) == 4
    agent_requests = []

    def agent_model(home, logs, request):
        agent_requests.append(request)
        return ModelCallResult(status="ok", content="ok", model="fake",
                               provider="fake", dry_run=True)

    with patch("jigga.runtime.agent.call_model", agent_model), \
         patch("jigga.runtime.model_router.call_model",
               lambda *a, **k: _summary_result("earlier: m0-m2 discussed")):
        assert main(["--home", str(tmp_path), "webchat", "send", "--wait",
                     "--text", "and what did we decide?"]) == 0

    [user] = [i for r in agent_requests for i in r.items if i.role == "user"]
    assert "Earlier in this conversation (summary)" in user.content
    assert "earlier: m0-m2 discussed" in user.content
    assert "Recent conversation in this thread" in user.content
    assert "and what did we decide?" in user.content


# --- conversations listing (the chat page's thread list) --------------------------


def test_list_conversations_groups_and_sorts_newest_first(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    webchat.append_inbound(paths.home, "old thread msg")                       # web
    webchat.append_inbound(paths.home, "lead q", conversation_id="marketing_lead",
                           target_agent="marketing_lead")
    webchat.send_message(paths.home, "marketing_lead", "lead a")
    convs = webchat.list_conversations(paths.home)
    assert [c["conversation_id"] for c in convs] == ["marketing_lead", "web"]  # newest first
    lead = convs[0]
    assert lead["agent"] == "marketing_lead"     # attributed from the picker target
    assert lead["count"] == 2
    assert lead["last_text"] == "lead a"
    assert lead["last_sender"] == "agent"
    assert convs[1]["agent"] is None             # default thread has no target


def test_list_conversations_last_is_by_timestamp_not_file_order(tmp_path: Path) -> None:
    """An outbox reply older than the latest inbox message must not win 'last'
    just because outbox is read second."""
    paths = init_runtime(tmp_path)
    channel_dir = paths.home / "channels" / "webchat"
    channel_dir.mkdir(parents=True)
    rows = [("inbox.jsonl", "you", "newest question", "2026-06-07T12:00:00Z"),
            ("outbox.jsonl", "agent", "older answer", "2026-06-07T11:00:00Z")]
    for name, sender, text, ts in rows:
        with (channel_dir / name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": text, "conversation_id": "web", "sender": sender,
                                 "text": text, "ts": ts}) + "\n")
    [conv] = webchat.list_conversations(paths.home)
    assert conv["last_text"] == "newest question"
    assert conv["count"] == 2


def test_list_conversations_empty(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    assert webchat.list_conversations(paths.home) == []


def test_cli_conversations_json(tmp_path: Path, capsys) -> None:
    paths = init_runtime(tmp_path)
    webchat.append_inbound(paths.home, "hello", conversation_id="chief-abc",
                           target_agent="chief")
    assert main(["--home", str(tmp_path), "webchat", "conversations", "--json"]) == 0
    [conv] = json.loads(capsys.readouterr().out)
    assert conv["conversation_id"] == "chief-abc"
    assert conv["agent"] == "chief"

"""Channel transcripts — conversation continuity for EXTERNAL channels.

Telegram (and future Slack/iMessage) keep messages on someone else's server,
so without a local transcript every conversation ran amnesiac. The listener
records inbound, the send primitive records outbound, and the adapter's
thread_context hook serves the same window+summary block webchat threads get."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from jigga.commands.init import init_runtime
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime import channel_transcript as ct
from jigga.runtime.channel_listener import ingest_once
from jigga.runtime.model_router import ModelCallResult


def _summary_result(text="SUMMARY", status="ok"):
    return ModelCallResult(status=status, content=text, model="fake",
                           provider="fake", dry_run=True)


def _enable_telegram(paths, *, default_agent="assistant") -> None:
    config = read_yaml(paths.config)
    config["channels"] = {"telegram": {"enabled": True, "allowed_chat_ids": ["111"],
                                       "default_agent": default_agent}}
    write_yaml(paths.config, config)


def _write_agent(paths, agent_id="assistant") -> None:
    write_yaml(paths.agents / f"{agent_id}.yaml",
               {"id": agent_id, "name": "Assistant", "role": "pa", "default": True,
                "permission_mode": "autonomous", "tools": []})


def _tg_msg(text, message_id, chat_id=111, sender="rj"):
    return {"channel": "telegram", "chat_id": chat_id, "sender": sender,
            "sender_id": chat_id, "text": text, "message_id": message_id}


def _set_channel_cfg(paths, channel, **kv) -> None:
    config = read_yaml(paths.config)
    entry = config.setdefault("channels", {}).setdefault(channel, {})
    entry.update(kv)
    write_yaml(paths.config, config)


# --- record / history --------------------------------------------------------


def test_record_and_history_filter_and_sort(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    ct.record(paths.home, "telegram", conversation_id=111, sender="rj",
              text="q1", direction="in", message_id=10)
    ct.record(paths.home, "telegram", conversation_id=111, sender="agent",
              text="a1", direction="out")
    ct.record(paths.home, "telegram", conversation_id=222, sender="sam",
              text="other chat", direction="in")
    entries = ct.history(paths.home, "telegram", 111)
    assert [(e["sender"], e["text"], e["direction"]) for e in entries] == \
        [("rj", "q1", "in"), ("agent", "a1", "out")]
    assert entries[0]["message_id"] == 10
    assert [e["text"] for e in ct.history(paths.home, "telegram", 222)] == ["other chat"]


def test_telegram_send_records_outbound(tmp_path: Path, monkeypatch) -> None:
    """The contract: a channel's send primitive records 'out' — covering agent
    tool replies AND runtime notices through the one funnel."""
    from jigga.runtime import telegram

    paths = init_runtime(tmp_path)
    (paths.secrets).mkdir(exist_ok=True)
    (paths.secrets / "telegram_bot_token").write_text("123:tok", encoding="utf-8")
    monkeypatch.setattr(telegram, "_api_call",
                        lambda token, method, params: {"ok": True, "result": {"message_id": 77}})
    result = telegram.send_message(paths.home, 111, "the reply")
    assert result["sent"] is True
    [entry] = ct.history(paths.home, "telegram", 111)
    assert entry["direction"] == "out" and entry["sender"] == "agent"
    assert entry["text"] == "the reply" and entry["message_id"] == 77


def test_listener_records_inbound_for_external_channels(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _write_agent(paths)
    _enable_telegram(paths)
    poll = {"status": "ok", "messages": [_tg_msg("hello bot", 10)]}
    with patch("jigga.runtime.telegram.poll_messages", return_value=poll), \
         patch("jigga.runtime.channel_listener.run_agent", lambda *a, **k: {"ok": True}):
        ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0)
    [entry] = ct.history(paths.home, "telegram", 111)
    assert (entry["sender"], entry["text"], entry["direction"]) == ("rj", "hello bot", "in")
    assert entry["message_id"] == 10


def test_listener_does_not_double_record_webchat(tmp_path: Path) -> None:
    """Webchat declares self_transcribed — its inbox IS the transcript."""
    from jigga.runtime import webchat

    paths = init_runtime(tmp_path)
    _write_agent(paths)
    config = read_yaml(paths.config)
    config["channels"] = {"webchat": {"enabled": True}}
    write_yaml(paths.config, config)
    webchat.append_inbound(paths.home, "browser message")
    with patch("jigga.runtime.channel_listener.run_agent", lambda *a, **k: {"ok": True}):
        ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0)
    assert not ct.transcript_path(paths.home, "webchat").exists()


def test_rejected_sender_not_recorded(tmp_path: Path) -> None:
    """Only gate-passing messages land in the transcript — allowlist spam
    must not become agent context."""
    paths = init_runtime(tmp_path)
    _write_agent(paths)
    _enable_telegram(paths)
    poll = {"status": "ok", "messages": [_tg_msg("spam", 11, chat_id=999, sender="rando")]}
    with patch("jigga.runtime.telegram.poll_messages", return_value=poll), \
         patch("jigga.runtime.channel_listener.run_agent", lambda *a, **k: {"ok": True}):
        ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0)
    assert ct.history(paths.home, "telegram", 999) == []


# --- window / summary / block ------------------------------------------------


def test_thread_tail_window_and_exclusion(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_channel_cfg(paths, "telegram", context_turns=2)
    ct.record(paths.home, "telegram", conversation_id=111, sender="rj",
              text="m1", direction="in", message_id=1)
    ct.record(paths.home, "telegram", conversation_id=111, sender="agent",
              text="m2", direction="out", message_id=2)
    ct.record(paths.home, "telegram", conversation_id=111, sender="rj",
              text="current", direction="in", message_id=3)
    tail = ct.thread_tail(paths.home, "telegram", 111, exclude_message_id=3)
    assert tail == "rj: m1\nagent: m2"   # full window despite exclusion
    assert ct.thread_tail(paths.home, "telegram", 111) == "agent: m2\nrj: current"


def test_roll_summary_folds_with_watermark(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_channel_cfg(paths, "telegram", context_turns=2)
    for i in range(5):
        ct.record(paths.home, "telegram", conversation_id=111, sender="rj",
                  text=f"m{i}", direction="in")
    calls = []

    def capture(home, logs, request):
        calls.append(request)
        return _summary_result("folded m0-m2")

    with patch("jigga.runtime.model_router.call_model", capture):
        assert ct.roll_summary(paths.home, paths.logs, "telegram", 111) == "folded m0-m2"
    prompt = calls[0].items[-1].content
    assert "m0" in prompt and "m2" in prompt and "m3" not in prompt

    def explode(*_a, **_k):
        raise AssertionError("re-called without new overflow")

    with patch("jigga.runtime.model_router.call_model", explode):
        assert ct.roll_summary(paths.home, paths.logs, "telegram", 111) == "folded m0-m2"


def test_roll_summary_failure_keeps_state(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _set_channel_cfg(paths, "telegram", context_turns=2)
    for i in range(5):
        ct.record(paths.home, "telegram", conversation_id=111, sender="rj",
                  text=f"m{i}", direction="in")
    with patch("jigga.runtime.model_router.call_model", lambda *a, **k: _summary_result("good")):
        ct.roll_summary(paths.home, paths.logs, "telegram", 111)
    before = ct.load_summary(paths.home, "telegram", 111)
    ct.record(paths.home, "telegram", conversation_id=111, sender="rj",
              text="m5", direction="in")
    with patch("jigga.runtime.model_router.call_model",
               lambda *a, **k: _summary_result("", status="error")):
        assert ct.roll_summary(paths.home, paths.logs, "telegram", 111) == "good"
    assert ct.load_summary(paths.home, "telegram", 111) == before


def test_agent_prompt_carries_telegram_thread(tmp_path: Path) -> None:
    """End-to-end: a telegram follow-up runs with the conversation's context
    block in the user message — external channels stop being amnesiac."""
    paths = init_runtime(tmp_path)
    _write_agent(paths)
    _enable_telegram(paths)
    # an earlier exchange already in the transcript
    ct.record(paths.home, "telegram", conversation_id=111, sender="rj",
              text="list our channels", direction="in", message_id=1)
    ct.record(paths.home, "telegram", conversation_id=111, sender="agent",
              text="telegram and webchat", direction="out")
    requests = []

    def agent_model(home, logs, request):
        requests.append(request)
        return ModelCallResult(status="ok", content="ok", model="fake",
                               provider="fake", dry_run=True)

    poll = {"status": "ok", "messages": [_tg_msg("tell me more about the second", 2)]}
    with patch("jigga.runtime.telegram.poll_messages", return_value=poll), \
         patch("jigga.runtime.agent.call_model", agent_model):
        ingest_once(paths.home, paths.logs, paths.tasks, paths.agents, long_poll_seconds=0)

    [user] = [i for r in requests for i in r.items if i.role == "user"]
    assert "Recent conversation in this thread" in user.content
    assert "rj: list our channels" in user.content
    assert "agent: telegram and webchat" in user.content
    # the current message appears once (task body), not duplicated via history
    assert user.content.count("tell me more about the second") == 1


# --- archival ------------------------------------------------------------------


def _age_first_line(paths, channel, days_old) -> None:
    path = ct.transcript_path(paths.home, channel)
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[0])
    entry["ts"] = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    lines[0] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_archive_moves_old_prefix_and_keeps_survivors(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    ct.record(paths.home, "telegram", conversation_id=111, sender="rj",
              text="ancient", direction="in")
    ct.record(paths.home, "telegram", conversation_id=111, sender="rj",
              text="recent", direction="in")
    _age_first_line(paths, "telegram", 40)
    assert ct.archive_transcripts_for(paths.home, "telegram") == 1
    assert [e["text"] for e in ct.history(paths.home, "telegram", 111)] == ["recent"]
    archive = paths.home / "channels" / "telegram" / "archive"
    archived = [json.loads(line) for f in archive.glob("transcript-*.jsonl")
                for line in f.read_text(encoding="utf-8").splitlines()]
    assert [e["text"] for e in archived] == ["ancient"]


def test_archive_retention_zero_disables_and_dry_run(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    ct.record(paths.home, "telegram", conversation_id=111, sender="rj",
              text="ancient", direction="in")
    _age_first_line(paths, "telegram", 40)
    _set_channel_cfg(paths, "telegram", retention_days=0)
    assert ct.archive_transcripts_for(paths.home, "telegram") == 0
    _set_channel_cfg(paths, "telegram", retention_days=30)
    before = ct.transcript_path(paths.home, "telegram").read_text(encoding="utf-8")
    assert ct.archive_transcripts_for(paths.home, "telegram", dry_run=True) == 1
    assert ct.transcript_path(paths.home, "telegram").read_text(encoding="utf-8") == before


def test_archive_all_discovers_channels_and_skips_webchat(tmp_path: Path) -> None:
    from jigga.runtime import webchat

    paths = init_runtime(tmp_path)
    ct.record(paths.home, "telegram", conversation_id=111, sender="rj",
              text="ancient", direction="in")
    _age_first_line(paths, "telegram", 40)
    webchat.append_inbound(paths.home, "webchat stays")     # no transcript.jsonl → not scanned
    results = ct.archive_all(paths.home)
    assert results == {"telegram": 1}
    assert [e["text"] for e in webchat.history(paths.home)] == ["webchat stays"]


def test_compaction_sweep_includes_channel_transcripts(tmp_path: Path) -> None:
    from jigga.runtime.compaction import compact_memory

    paths = init_runtime(tmp_path)
    ct.record(paths.home, "telegram", conversation_id=111, sender="rj",
              text="ancient", direction="in")
    _age_first_line(paths, "telegram", 40)
    result = compact_memory(paths.home)
    assert result["channel_transcripts_archived"] == {"telegram": 1}

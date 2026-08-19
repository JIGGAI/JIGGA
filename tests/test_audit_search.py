"""`jigga audit --contains` — free-text search over the audit log.

jiggaview's Events page can filter by type, status, agent and actor, but the
thing you usually have is a fragment: half an error sentence, a file path, a
conversation id. Which key holds it differs per event type, so the search has to
run over the whole event — and it has to run in CORE, not over whatever rows the
UI already fetched. Filtering a fetched page reports "no matches" while the
match sits a page deeper in a log of 100k+ events.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.runtime.audit import append_event
from jigga.runtime.audit_query import query_events


def _log(tmp_path: Path):
    logs = tmp_path / "logs"
    append_event(logs, "channel.ingest_error", status="error",
                 error="Telegram getUpdates failed: HTTP 409 Conflict")
    append_event(logs, "agent.tool_call", agent="chief", action="filesystem.read_file",
                 input="{'path': '/home/control/notes.md'}")
    append_event(logs, "model.call.failed", status="error", agent="writer",
                 error="rate limited")
    return logs


def test_it_searches_inside_details_not_just_the_type(tmp_path: Path) -> None:
    # The needle is almost always in `details` — an error sentence, a path — and
    # a search that only looked at `type` would miss every one of them.
    logs = _log(tmp_path)
    hits = query_events(logs, contains="notes.md")
    assert [e["type"] for e in hits] == ["agent.tool_call"]


def test_it_is_case_insensitive(tmp_path: Path) -> None:
    logs = _log(tmp_path)
    assert len(query_events(logs, contains="TELEGRAM")) == 1
    assert len(query_events(logs, contains="telegram")) == 1


def test_it_matches_the_event_type_and_status_too(tmp_path: Path) -> None:
    logs = _log(tmp_path)
    assert len(query_events(logs, contains="model.call")) == 1
    assert len(query_events(logs, contains="error")) >= 2


def test_it_combines_with_the_other_filters(tmp_path: Path) -> None:
    # Search NARROWS an existing filter; it does not replace it.
    logs = _log(tmp_path)
    assert len(query_events(logs, contains="error")) >= 2
    assert [e["agent"] if "agent" in e else e["details"].get("agent")
            for e in query_events(logs, status="error", contains="rate")] == ["writer"]


def test_an_empty_or_blank_search_filters_nothing(tmp_path: Path) -> None:
    # An empty search box must not empty the page.
    logs = _log(tmp_path)
    everything = len(query_events(logs))
    assert len(query_events(logs, contains="")) == everything
    assert len(query_events(logs, contains="   ")) == everything
    assert len(query_events(logs, contains=None)) == everything


def test_no_match_is_empty_not_everything(tmp_path: Path) -> None:
    # Failing open on a search would show a full page and read as "these are
    # your matches".
    logs = _log(tmp_path)
    assert query_events(logs, contains="zzz-not-present") == []


def test_the_limit_still_keeps_the_most_recent_matches(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    for i in range(5):
        append_event(logs, "agent.wake", agent="chief", note=f"wake {i}")
    hits = query_events(logs, contains="wake", limit=2)
    assert [h["details"]["note"] for h in hits] == ["wake 3", "wake 4"]


def test_a_torn_line_does_not_break_the_search(tmp_path: Path) -> None:
    logs = _log(tmp_path)
    with (logs / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"type": "half-written", "details": {\n')
    assert len(query_events(logs, contains="telegram")) == 1


def test_the_cli_exposes_it(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    _log(tmp_path)
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "audit", "--contains", "notes.md", "--json"]) == 0
    events = json.loads(capsys.readouterr().out)
    assert [e["type"] for e in events] == ["agent.tool_call"]


@pytest.mark.parametrize("needle", ["409", "conflict", "getupdates"])
def test_a_fragment_of_an_error_sentence_finds_it(tmp_path: Path, needle: str) -> None:
    # The realistic case: you remember part of what it said, not which field it
    # was in or what the event was called.
    logs = _log(tmp_path)
    assert [e["type"] for e in query_events(logs, contains=needle)] == ["channel.ingest_error"]

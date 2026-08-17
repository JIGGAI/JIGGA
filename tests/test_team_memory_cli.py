"""A team's durable memory is reachable from the CLI.

Agents have written to `shared-context/memory/team.jsonl` since D2, through the
`memory.remember` capability. Humans had no way in: `memory search` could find
an entry, but nothing could list what a team knows, add to it, or curate the
pinned subset without hand-editing a jsonl. That also blocked any UI, which
reaches the runtime only through the CLI.

`team memory list|add|pin` is the same store from the other side.
"""

from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.models import TeamConfig
from jigga.runtime.team_memory import append_team_memory, read_pinned
from jigga.runtime.workspaces import scaffold_workspace


def _team(tmp_path: Path, team_id: str = "mt"):
    paths = init_runtime(tmp_path)
    team = TeamConfig(id=team_id, name="MT", agents=[{"id": "mt-lead", "role": "lead"}],
                      routing={"default_assignee": "mt-lead"})
    scaffold_workspace(paths.home, team)
    return paths


def _json(capsys):
    return json.loads(capsys.readouterr().out)


# --- add ---------------------------------------------------------------------


def test_add_records_type_tags_and_a_human_actor(tmp_path: Path, capsys) -> None:
    _team(tmp_path)
    assert main(["--home", str(tmp_path), "team", "memory", "add", "mt", "--json",
                 "--text", "Launch ships Tuesday.", "--type", "decision",
                 "--tag", "launch", "--tag", "timing"]) == 0
    entry = _json(capsys)
    assert entry["type"] == "decision" and entry["tags"] == ["launch", "timing"]
    # Human vs agent has to stay distinguishable per write — an agent's entry
    # carries {"agent": id} here, so a CLI write must not look like one.
    assert entry["source"] == {"actor": "user", "via": "cli"}


def test_add_is_audited(tmp_path: Path, capsys) -> None:
    _team(tmp_path)
    assert main(["--home", str(tmp_path), "team", "memory", "add", "mt",
                 "--text", "Prefer plain English."]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "audit", "--type", "team.memory_added", "--json"]) == 0
    events = _json(capsys)
    assert len(events) == 1 and events[0]["details"]["team"] == "mt"


def test_add_rejects_empty_text(tmp_path: Path, capsys) -> None:
    _team(tmp_path)
    assert main(["--home", str(tmp_path), "team", "memory", "add", "mt", "--text", "   "]) == 1
    assert "Nothing to remember" in capsys.readouterr().out


def test_add_is_immediately_searchable(tmp_path: Path, capsys) -> None:
    # The write goes through the same append path `memory.remember` uses, so the
    # search index picks it up without a reindex.
    _team(tmp_path)
    assert main(["--home", str(tmp_path), "team", "memory", "add", "mt",
                 "--text", "The platypus protocol governs launches."]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "memory", "search", "platypus",
                 "--team", "mt", "--json"]) == 0
    assert _json(capsys)[0]["layer"] == "team:mt"


# --- list --------------------------------------------------------------------


def test_list_returns_entries_oldest_first(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path)
    append_team_memory(paths.home, "mt", text="first")
    append_team_memory(paths.home, "mt", text="second")
    assert main(["--home", str(tmp_path), "team", "memory", "list", "mt", "--json"]) == 0
    assert [e["text"] for e in _json(capsys)] == ["first", "second"]


def test_list_limit_keeps_the_most_recent(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path)
    for text in ("a", "b", "c"):
        append_team_memory(paths.home, "mt", text=text)
    assert main(["--home", str(tmp_path), "team", "memory", "list", "mt",
                 "--limit", "2", "--json"]) == 0
    assert [e["text"] for e in _json(capsys)] == ["b", "c"]


def test_list_pinned_is_the_curated_subset(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path)
    keeper = append_team_memory(paths.home, "mt", text="keep me")
    append_team_memory(paths.home, "mt", text="noise")
    assert main(["--home", str(tmp_path), "team", "memory", "pin", "mt", keeper["id"]]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "team", "memory", "list", "mt",
                 "--pinned", "--json"]) == 0
    assert [e["text"] for e in _json(capsys)] == ["keep me"]


def test_list_of_an_empty_store_is_an_empty_list(tmp_path: Path, capsys) -> None:
    _team(tmp_path)
    assert main(["--home", str(tmp_path), "team", "memory", "list", "mt", "--json"]) == 0
    assert _json(capsys) == []


# --- pin ---------------------------------------------------------------------


def test_pin_matches_an_id_prefix_and_is_audited(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path)
    entry = append_team_memory(paths.home, "mt", text="prefix me")
    assert main(["--home", str(tmp_path), "team", "memory", "pin", "mt",
                 entry["id"][:8], "--json"]) == 0
    assert _json(capsys)["id"] == entry["id"]
    assert main(["--home", str(tmp_path), "audit", "--type", "team.memory_pinned", "--json"]) == 0
    assert len(_json(capsys)) == 1


def test_pinning_twice_does_not_duplicate(tmp_path: Path, capsys) -> None:
    # pinned.jsonl feeds the agent context pack — a duplicate there is the same
    # fact twice in a prompt, and a UI's pin button is trivially clickable twice.
    paths = _team(tmp_path)
    entry = append_team_memory(paths.home, "mt", text="once")
    assert main(["--home", str(tmp_path), "team", "memory", "pin", "mt", entry["id"]]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "team", "memory", "pin", "mt", entry["id"],
                 "--json"]) == 0
    assert _json(capsys)["already_pinned"] is True
    assert len(read_pinned(paths.home, "mt")) == 1

    # …and the second pin is not re-audited, since nothing changed.
    assert main(["--home", str(tmp_path), "audit", "--type", "team.memory_pinned", "--json"]) == 0
    assert len(_json(capsys)) == 1


def test_pin_unknown_entry_exits_nonzero(tmp_path: Path, capsys) -> None:
    _team(tmp_path)
    assert main(["--home", str(tmp_path), "team", "memory", "pin", "mt", "mem_nope"]) == 1
    assert "No memory entry matching" in capsys.readouterr().out


# --- a missing workspace is not created by accident ---------------------------


def test_a_typod_team_does_not_get_a_ghost_workspace(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "team", "memory", "add", "ghost",
                 "--text", "hello", "--json"]) == 1
    assert "jigga team init ghost" in _json(capsys)["error"]
    assert not (tmp_path / "workspaces" / "ghost").exists()

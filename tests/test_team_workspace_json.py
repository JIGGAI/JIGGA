"""`team workspace` can answer in JSON.

`team files --json` returns a fixed manifest — the files a team is *supposed*
to have, each marked required/missing. That is the right answer for "is this
team set up correctly" and the wrong one for "what is actually in here": notes,
role memory, shared context, and agent outputs accumulate at runtime and appear
in none of it.

`team workspace` already walked the real tree, but printed only for humans, so
the one surface that knows what a workspace contains was unreachable from
anything that parses. Now it takes `--json`, with size and mtime — enough to
render a file browser without the caller reading `~/.jigga` behind the CLI's
back.
"""

from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.models import TeamConfig
from jigga.runtime.workspaces import scaffold_workspace


def _team(tmp_path: Path, team_id: str = "mt"):
    paths = init_runtime(tmp_path)
    team = TeamConfig(id=team_id, name="MT", agents=[{"id": "mt-lead", "role": "lead"}],
                      routing={"default_assignee": "mt-lead"})
    scaffold_workspace(paths.home, team)
    return paths


def _workspace(tmp_path: Path, capsys, team_id: str = "mt") -> dict:
    assert main(["--home", str(tmp_path), "team", "workspace", team_id, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_lists_the_real_tree_not_the_required_manifest(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path)
    # A runtime file no manifest knows about.
    note = paths.home / "workspaces" / "mt" / "notes" / "kickoff.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# Kickoff\n", encoding="utf-8")

    names = [f["name"] for f in _workspace(tmp_path, capsys)["files"]]
    assert "notes/kickoff.md" in names
    assert "TEAM.md" in names

    # …and the manifest listing genuinely does not have it, which is the point.
    assert main(["--home", str(tmp_path), "team", "files", "mt", "--json"]) == 0
    assert "notes/kickoff.md" not in [f["name"] for f in json.loads(capsys.readouterr().out)]


def test_entries_carry_size_and_mtime_and_posix_relative_names(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path)
    nested = paths.home / "workspaces" / "mt" / "shared-context" / "memory" / "team.jsonl"
    nested.parent.mkdir(parents=True, exist_ok=True)
    nested.write_text('{"id": "mem_1"}\n', encoding="utf-8")

    entry = next(f for f in _workspace(tmp_path, capsys)["files"]
                 if f["name"] == "shared-context/memory/team.jsonl")
    assert entry["bytes"] == 16
    assert entry["modified"].endswith("+00:00")


def test_directories_are_not_listed(tmp_path: Path, capsys) -> None:
    _team(tmp_path)
    files = _workspace(tmp_path, capsys)["files"]
    assert files and all("." in Path(f["name"]).name for f in files)
    assert "notes" not in [f["name"] for f in files]


def test_a_symlink_escaping_the_workspace_is_not_listed(tmp_path: Path, capsys) -> None:
    # `team file get` confines to the workspace root, so an escaping symlink
    # would be advertised here and then refuse to open.
    paths = _team(tmp_path)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("nope", encoding="utf-8")
    link = paths.home / "workspaces" / "mt" / "leak.txt"
    link.symlink_to(outside)

    names = [f["name"] for f in _workspace(tmp_path, capsys)["files"]]
    assert "leak.txt" not in names
    assert main(["--home", str(tmp_path), "team", "file", "get", "mt", "leak.txt"]) != 0


def test_a_symlink_inside_the_workspace_is_listed(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path)
    root = paths.home / "workspaces" / "mt"
    (root / "notes").mkdir(parents=True, exist_ok=True)
    (root / "notes" / "real.md").write_text("real\n", encoding="utf-8")
    (root / "alias.md").symlink_to(root / "notes" / "real.md")

    assert "alias.md" in [f["name"] for f in _workspace(tmp_path, capsys)["files"]]


def test_missing_workspace_answers_json_and_exits_nonzero(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "team", "workspace", "ghost", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)  # JSON in, JSON out — even on the error path
    assert payload["files"] == [] and "jigga team init ghost" in payload["error"]


def test_human_output_is_unchanged(tmp_path: Path, capsys) -> None:
    paths = _team(tmp_path)
    assert main(["--home", str(tmp_path), "team", "workspace", "mt"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == str(paths.home / "workspaces" / "mt")
    assert all(line.startswith("  ") for line in out[1:])

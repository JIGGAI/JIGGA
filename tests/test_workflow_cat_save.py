"""A workflow's yaml can be read and written from the CLI.

Everything else about workflows was already reachable — list, plan, run, runs,
resume — but the *document* was not: there was no way to see what a workflow
actually says, or to change it, without opening `~/.jigga/workflows/<id>.yaml`
in an editor. That is the one gap that kept jiggaview from showing a workflow
at all, since the UI is only allowed to reach the runtime through the CLI.

The write path validates BEFORE it writes, using the same checks `jigga
validate` runs. A recipe is an inert template — saving a broken one is caught
the next time someone scaffolds it. A workflow is live: the supervisor picks it
up on the next tick, so a broken save is a broken runtime, and the file is left
untouched instead.
"""

from __future__ import annotations

import json
from pathlib import Path

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml

LINEAR = {
    "id": "weekly_digest",
    "name": "Weekly Digest",
    "status": "draft",
    "steps": [{"id": "draft", "agent": "writer", "action": "draft_digest", "output": "digest.md"}],
}


def _seed(tmp_path: Path, doc: dict = LINEAR, *, filename: str | None = None) -> Path:
    paths = init_runtime(tmp_path)
    target = paths.workflows / f"{filename or doc['id']}.yaml"
    write_yaml(target, doc)
    return target


# --- read --------------------------------------------------------------------


def test_cat_prints_the_yaml(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    assert main(["--home", str(tmp_path), "workflow", "cat", "weekly_digest"]) == 0
    out = capsys.readouterr().out
    assert "id: weekly_digest" in out and "draft_digest" in out


def test_cat_unknown_workflow_exits_nonzero(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "workflow", "cat", "nope"]) == 1
    assert "not found" in capsys.readouterr().out


def test_cat_finds_a_workflow_whose_filename_differs_from_its_id(tmp_path: Path, capsys) -> None:
    # A workflow is keyed by the `id:` inside the document, not by its
    # filename, so a hand-made file can disagree — cat must still find it.
    _seed(tmp_path, filename="renamed-by-hand")
    assert main(["--home", str(tmp_path), "workflow", "cat", "weekly_digest"]) == 0
    assert "id: weekly_digest" in capsys.readouterr().out


# --- write -------------------------------------------------------------------


def test_save_roundtrips_and_is_audited(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    assert main(["--home", str(tmp_path), "workflow", "cat", "weekly_digest"]) == 0
    edited = capsys.readouterr().out.replace("Weekly Digest", "Weekly Roundup")

    assert main(["--home", str(tmp_path), "workflow", "save", "weekly_digest",
                 "--content", edited, "--json"]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["created"] is False
    assert saved["path"].endswith("workflows/weekly_digest.yaml")

    assert main(["--home", str(tmp_path), "workflow", "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "Weekly Roundup"

    assert main(["--home", str(tmp_path), "audit", "--type", "workflow.saved", "--json"]) == 0
    events = json.loads(capsys.readouterr().out)
    assert [e["details"]["workflow"] for e in events] == ["weekly_digest"]


def test_save_creates_a_new_workflow(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "workflow", "save", "fresh", "--json",
                 "--content", "id: fresh\nname: Fresh\n"]) == 0
    assert json.loads(capsys.readouterr().out)["created"] is True
    assert (tmp_path / "workflows" / "fresh.yaml").exists()


def test_save_reads_stdin_when_content_is_omitted(tmp_path: Path, monkeypatch, capsys) -> None:
    import io

    init_runtime(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("id: piped\nname: Piped\n"))
    assert main(["--home", str(tmp_path), "workflow", "save", "piped"]) == 0
    assert "created" in capsys.readouterr().out


# --- rejected saves leave the file alone --------------------------------------


def _unchanged(tmp_path: Path, capsys) -> bool:
    assert main(["--home", str(tmp_path), "workflow", "cat", "weekly_digest"]) == 0
    return "Weekly Digest" in capsys.readouterr().out


def test_invalid_yaml_is_rejected(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    assert main(["--home", str(tmp_path), "workflow", "save", "weekly_digest",
                 "--content", "id: [unclosed"]) == 1
    assert "not valid yaml" in capsys.readouterr().out
    assert _unchanged(tmp_path, capsys)


def test_missing_id_is_rejected(tmp_path: Path, capsys) -> None:
    _seed(tmp_path)
    assert main(["--home", str(tmp_path), "workflow", "save", "weekly_digest",
                 "--content", "name: No Id\n"]) == 1
    assert "missing `id:`" in capsys.readouterr().out
    assert _unchanged(tmp_path, capsys)


def test_id_mismatch_is_rejected(tmp_path: Path, capsys) -> None:
    # Saving a doc whose id disagrees with the target orphans the file: the
    # loader keys by the id inside, so the two would name different workflows.
    _seed(tmp_path)
    assert main(["--home", str(tmp_path), "workflow", "save", "weekly_digest",
                 "--content", "id: something_else\nname: Else\n"]) == 1
    assert "does not match" in capsys.readouterr().out
    assert _unchanged(tmp_path, capsys)


def test_a_broken_dag_is_rejected_before_it_reaches_the_supervisor(tmp_path: Path, capsys) -> None:
    # The same graph check `jigga validate` runs — a cycle never completes, and
    # a saved cycle would be picked up on the next supervisor tick.
    init_runtime(tmp_path)
    cyclic = (
        "id: cyc\nname: Cyc\n"
        "nodes:\n"
        "- {id: a, type: tool, action: filesystem.read_file}\n"
        "- {id: b, type: tool, action: filesystem.read_file}\n"
        "edges:\n"
        "- {from: a, to: b, on: success}\n"
        "- {from: b, to: a, on: success}\n"
    )
    assert main(["--home", str(tmp_path), "workflow", "save", "cyc", "--content", cyclic]) == 1
    assert "cycle" in capsys.readouterr().out
    assert not (tmp_path / "workflows" / "cyc.yaml").exists()


def test_a_path_traversing_id_is_rejected(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    assert main(["--home", str(tmp_path), "workflow", "save", "../escape",
                 "--content", "id: ../escape\nname: X\n"]) == 1
    assert "Invalid workflow id" in capsys.readouterr().out

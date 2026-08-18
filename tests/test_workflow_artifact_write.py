"""A human can correct a run's file before approving it.

Parking a run on `human_approval` is only half a review: you could read what the
model produced and say yes or no, but not fix the one wrong sentence — the only
way to change a headline was to deny the approval and re-run the whole graph.
`workflow artifact-save` closes that, under the same confinement `workflow
artifact` reads with.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import write_json
from jigga.runtime.workflow_engine import read_run_artifact, write_run_artifact


def _run(home: Path, *, status: str = "awaiting_approval", run_id: str = "workflow_run_a1") -> Path:
    run_dir = home / "runs" / "workflows" / "team_launch" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json(run_dir / "run.json", {
        "id": run_id, "workflow_id": "team_launch", "engine": "v2", "status": status,
        "nodes": {"copy": {"status": "done"}}, "outputs": {},
    })
    (run_dir / "copy.md").write_text("draft the model wrote\n", encoding="utf-8")
    return run_dir


def test_it_replaces_the_file_and_reads_back(tmp_path: Path) -> None:
    from jigga.core.paths import get_paths

    init_runtime(tmp_path)
    _run(tmp_path)
    paths = get_paths(tmp_path)

    result = write_run_artifact(paths, "workflow_run_a1", "copy.md", "the fixed headline\n")

    assert result["created"] is False
    assert read_run_artifact(paths, "workflow_run_a1", "copy.md") == "the fixed headline\n"


def test_a_running_run_is_refused(tmp_path: Path) -> None:
    # Nodes write their outputs as they finish, so an edit mid-run races a node
    # with nothing to arbitrate it — whoever lands last wins by milliseconds.
    from jigga.core.paths import get_paths

    init_runtime(tmp_path)
    _run(tmp_path, status="running")

    with pytest.raises(ValueError, match="running"):
        write_run_artifact(get_paths(tmp_path), "workflow_run_a1", "copy.md", "nope")
    assert (tmp_path / "runs" / "workflows" / "team_launch" / "workflow_run_a1" / "copy.md"
            ).read_text() == "draft the model wrote\n"


@pytest.mark.parametrize("status", ["completed", "failed", "awaiting_approval"])
def test_a_settled_run_is_editable(tmp_path: Path, status: str) -> None:
    from jigga.core.paths import get_paths

    init_runtime(tmp_path)
    _run(tmp_path, status=status)
    write_run_artifact(get_paths(tmp_path), "workflow_run_a1", "copy.md", "edited\n")
    assert read_run_artifact(get_paths(tmp_path), "workflow_run_a1", "copy.md") == "edited\n"


def test_traversal_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    # An artifact name comes from a workflow definition, which a recipe can
    # install — so it is untrusted input on the write path exactly as on read.
    from jigga.core.paths import get_paths

    init_runtime(tmp_path)
    _run(tmp_path)
    victim = tmp_path / "config.yaml"
    before = victim.read_text(encoding="utf-8") if victim.exists() else None

    with pytest.raises(ValueError, match="escapes"):
        write_run_artifact(get_paths(tmp_path), "workflow_run_a1", "../../../../config.yaml", "x")
    assert (victim.read_text(encoding="utf-8") if victim.exists() else None) == before


def test_a_symlink_out_of_the_run_dir_is_refused(tmp_path: Path) -> None:
    # The name is confined by resolving, not by string-matching, so a link
    # planted in the run directory cannot be used to reach past it either.
    from jigga.core.paths import get_paths

    init_runtime(tmp_path)
    run_dir = _run(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("untouched\n", encoding="utf-8")
    (run_dir / "link.md").symlink_to(outside)

    with pytest.raises(ValueError, match="escapes"):
        write_run_artifact(get_paths(tmp_path), "workflow_run_a1", "link.md", "clobbered")
    assert outside.read_text(encoding="utf-8") == "untouched\n"


def test_an_unknown_run_is_an_error_not_a_new_directory(tmp_path: Path) -> None:
    from jigga.core.paths import get_paths

    init_runtime(tmp_path)
    with pytest.raises(ValueError, match="Run not found"):
        write_run_artifact(get_paths(tmp_path), "workflow_run_nope", "copy.md", "x")
    assert not (tmp_path / "runs" / "workflows" / "team_launch").exists()


def test_the_edit_is_audited_with_who_and_which_run(tmp_path: Path, capsys) -> None:
    # An artifact that a human rewrote is not what the model produced, and the
    # audit log is the only place that distinction survives.
    from jigga.core.paths import get_paths

    init_runtime(tmp_path)
    _run(tmp_path)
    write_run_artifact(get_paths(tmp_path), "workflow_run_a1", "copy.md", "human words\n")

    events = [json.loads(line) for line
              in (tmp_path / "logs" / "events.jsonl").read_text().splitlines() if line.strip()]
    written = [e for e in events if e["type"] == "workflow.artifact.written"]
    assert len(written) == 1
    assert written[0]["details"]["artifact"] == "copy.md"
    assert written[0]["details"]["run_id"] == "workflow_run_a1"
    assert written[0]["details"]["workflow"] == "team_launch"
    assert written[0]["actor"], "an unattributable edit is the thing this event exists to prevent"


# --- the CLI surface ---------------------------------------------------------


def test_cli_saves_from_content_and_prints_json(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    _run(tmp_path)
    assert main(["--home", str(tmp_path), "workflow", "artifact-save", "workflow_run_a1",
                 "copy.md", "--content", "from the cli\n", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["artifact"] == "copy.md" and payload["created"] is False

    assert main(["--home", str(tmp_path), "workflow", "artifact", "workflow_run_a1", "copy.md"]) == 0
    assert capsys.readouterr().out == "from the cli\n"


def test_cli_reads_stdin_when_content_is_omitted(tmp_path: Path, capsys, monkeypatch) -> None:
    import io

    init_runtime(tmp_path)
    _run(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("piped in\n"))
    assert main(["--home", str(tmp_path), "workflow", "artifact-save", "workflow_run_a1",
                 "copy.md"]) == 0
    capsys.readouterr()
    assert main(["--home", str(tmp_path), "workflow", "artifact", "workflow_run_a1", "copy.md"]) == 0
    assert capsys.readouterr().out == "piped in\n"


def test_cli_refuses_a_running_run_with_a_nonzero_exit(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    _run(tmp_path, status="running")
    assert main(["--home", str(tmp_path), "workflow", "artifact-save", "workflow_run_a1",
                 "copy.md", "--content", "x"]) == 1
    assert "running" in capsys.readouterr().out

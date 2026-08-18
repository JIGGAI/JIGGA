"""Run history, and the files a run produced.

`workflow runs` filtered to `engine: v2`, so eleven completed v1 runs sat in
`~/.jigga/runs/workflows/` while the command reported none — on the real box it
was 47. The history existed; nothing could see it.

A step's `output:` name becomes a file in the run directory
(`day_summary.md`, `calendar_events`), which is the closest thing JIGGA has to a
deliverable — so listing runs now lists what each one left behind, and one
command prints it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jigga.cli import main
from jigga.commands.init import init_runtime
from jigga.core.io import write_json
from jigga.runtime.workflow_engine import list_runs, read_run_artifact


def _run(tmp_path: Path, workflow: str, run_id: str, *, engine: str | None,
         artifacts: dict[str, str] | None = None, status: str = "completed") -> Path:
    run_dir = tmp_path / "runs" / "workflows" / workflow / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    record = {"id": run_id, "workflow_id": workflow, "status": status}
    if engine:
        record["engine"] = engine
    write_json(run_dir / "run.json", record)
    for name, body in (artifacts or {}).items():
        (run_dir / name).write_text(body, encoding="utf-8")
    return run_dir


def _paths(tmp_path: Path):
    from jigga.core.paths import get_paths
    return get_paths(tmp_path)


def test_both_engines_are_listed(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    _run(tmp_path, "old", "workflow_run_v1", engine=None)      # predates the field
    _run(tmp_path, "new", "workflow_run_v2", engine="v2")
    ids = {r["id"] for r in list_runs(_paths(tmp_path))}
    assert ids == {"workflow_run_v1", "workflow_run_v2"}


def test_a_run_without_the_field_is_reported_as_v1(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    _run(tmp_path, "old", "workflow_run_v1", engine=None)
    assert list_runs(_paths(tmp_path))[0]["engine"] == "v1"


def test_one_engine_can_still_be_asked_for(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    _run(tmp_path, "old", "workflow_run_v1", engine=None)
    _run(tmp_path, "new", "workflow_run_v2", engine="v2")
    assert [r["id"] for r in list_runs(_paths(tmp_path), engine="v2")] == ["workflow_run_v2"]


def test_each_run_lists_what_it_produced(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    _run(tmp_path, "wf", "workflow_run_a", engine=None,
         artifacts={"day_summary.md": "# Today\n", "calendar_events": "[]"})
    artifacts = list_runs(_paths(tmp_path))[0]["artifacts"]
    assert {a["name"] for a in artifacts} == {"day_summary.md", "calendar_events"}
    assert all(a["bytes"] > 0 and a["modified"].endswith("+00:00") for a in artifacts)


def test_the_run_record_is_not_an_artifact(tmp_path: Path) -> None:
    # run.json is the run's own bookkeeping, not something the workflow made.
    init_runtime(tmp_path)
    _run(tmp_path, "wf", "workflow_run_a", engine="v2", artifacts={"out.md": "x"})
    assert [a["name"] for a in list_runs(_paths(tmp_path))[0]["artifacts"]] == ["out.md"]


def test_active_only_still_filters_by_status(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    _run(tmp_path, "wf", "workflow_run_done", engine="v2", status="completed")
    _run(tmp_path, "wf", "workflow_run_live", engine="v2", status="awaiting_approval")
    assert [r["id"] for r in list_runs(_paths(tmp_path), active_only=True)] == ["workflow_run_live"]


def test_a_corrupt_run_file_does_not_hide_the_others(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    _run(tmp_path, "wf", "workflow_run_ok", engine="v2")
    bad = tmp_path / "runs" / "workflows" / "wf" / "workflow_run_bad"
    bad.mkdir(parents=True)
    (bad / "run.json").write_text("{ not json", encoding="utf-8")
    assert [r["id"] for r in list_runs(_paths(tmp_path))] == ["workflow_run_ok"]


# --- reading one artifact -----------------------------------------------------


def test_an_artifact_can_be_read(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    _run(tmp_path, "wf", "workflow_run_a", engine=None, artifacts={"day_summary.md": "# Today\n"})
    assert read_run_artifact(_paths(tmp_path), "workflow_run_a", "day_summary.md") == "# Today\n"


def test_a_missing_artifact_is_none_not_an_error(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    _run(tmp_path, "wf", "workflow_run_a", engine=None)
    assert read_run_artifact(_paths(tmp_path), "workflow_run_a", "nope.md") is None


def test_an_artifact_name_cannot_escape_the_run_directory(tmp_path: Path) -> None:
    # The name comes from a workflow definition — data, not a promise.
    init_runtime(tmp_path)
    _run(tmp_path, "wf", "workflow_run_a", engine=None)
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes the run directory"):
        read_run_artifact(_paths(tmp_path), "workflow_run_a", "../../../../secret.txt")


def test_the_cli_prints_an_artifact_and_refuses_traversal(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    _run(tmp_path, "wf", "workflow_run_a", engine=None, artifacts={"out.md": "hello\n"})
    assert main(["--home", str(tmp_path), "workflow", "artifact", "workflow_run_a", "out.md"]) == 0
    assert capsys.readouterr().out == "hello\n"

    assert main(["--home", str(tmp_path), "workflow", "artifact", "workflow_run_a",
                 "../../etc/passwd"]) == 1
    assert "escapes the run directory" in capsys.readouterr().out


def test_the_cli_lists_runs_with_their_artifacts(tmp_path: Path, capsys) -> None:
    init_runtime(tmp_path)
    _run(tmp_path, "wf", "workflow_run_a", engine=None, artifacts={"out.md": "x"})
    assert main(["--home", str(tmp_path), "workflow", "runs", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["artifacts"][0]["name"] == "out.md"

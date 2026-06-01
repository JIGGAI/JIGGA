"""core/io JSONL helpers — the shared read/append/rewrite used by the audit log,
memory, proposals, and the handoff decision log. read_jsonl must tolerate a
corrupt/partial tail (typical after a crashed append) rather than throw, and
rewrite_jsonl must replace atomically."""

from __future__ import annotations

from pathlib import Path

from jigga.core.io import append_jsonl, read_jsonl, rewrite_jsonl


def test_read_jsonl_skips_blank_and_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    path.write_text('{"a": 1}\n\n{ this is not json\n{"b": 2}\n', encoding="utf-8")
    # a single bad line (e.g. a half-written record after a crash) must not lose
    # the good records or raise
    assert read_jsonl(path) == [{"a": 1}, {"b": 2}]


def test_read_jsonl_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_jsonl(tmp_path / "nope.jsonl") == []


def test_append_then_read_roundtrip_and_parent_creation(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "log.jsonl"   # parent doesn't exist yet
    append_jsonl(path, {"n": 1})
    append_jsonl(path, {"n": 2})
    assert read_jsonl(path) == [{"n": 1}, {"n": 2}]


def test_rewrite_jsonl_replaces_contents(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    append_jsonl(path, {"old": True})
    rewrite_jsonl(path, [{"new": 1}, {"new": 2}])
    assert read_jsonl(path) == [{"new": 1}, {"new": 2}]   # old content gone

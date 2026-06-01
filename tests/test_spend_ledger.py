from __future__ import annotations

import json
from pathlib import Path

from jigga.commands.init import init_runtime
from jigga.runtime.audit import append_event
from jigga.runtime.cost import agent_spend
from jigga.runtime.spend_ledger import _ledger_path, window_spend


def _seed(logs: Path, agent_id: str, cost: float) -> None:
    append_event(logs, "model.call", agent_id=agent_id, provider="p", model="m",
                 dry_run=True, cost_usd=cost, input_tokens=10, output_tokens=5)


def test_window_spend_matches_full_scan(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _seed(paths.logs, "alpha", 0.10)
    _seed(paths.logs, "alpha", 0.20)
    _seed(paths.logs, "beta", 9.00)
    assert window_spend(paths.home, paths.logs, "alpha", window="all") == 0.30
    assert window_spend(paths.home, paths.logs, "alpha", window="all") == agent_spend(paths.logs, "alpha")


def test_incremental_tail_picks_up_new_events(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _seed(paths.logs, "alpha", 0.10)
    assert window_spend(paths.home, paths.logs, "alpha", window="all") == 0.10
    # a later call appends; the ledger should fold only the new bytes in
    _seed(paths.logs, "alpha", 0.25)
    assert window_spend(paths.home, paths.logs, "alpha", window="all") == 0.35
    # the ledger advanced its offset rather than re-reading from zero
    data = json.loads(_ledger_path(paths.home, "alpha").read_text())
    assert data["offset"] == (paths.logs / "events.jsonl").stat().st_size


def test_self_heals_when_log_cleared(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _seed(paths.logs, "alpha", 0.40)
    assert window_spend(paths.home, paths.logs, "alpha", window="all") == 0.40
    # clearing the audit log (source of truth) must reset derived spend
    (paths.logs / "events.jsonl").unlink()
    assert window_spend(paths.home, paths.logs, "alpha", window="all") == 0.0


def test_rolling_window_drops_old_entries(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    # two model.call events for each agent: one ancient, one far-future
    def _line(agent: str, time_iso: str, cost: float) -> str:
        return json.dumps({"id": f"{agent}{time_iso}", "time": time_iso, "type": "model.call",
                           "status": "ok", "details": {"agent_id": agent, "cost_usd": cost}})
    rows = [_line("alpha", "2000-01-01T00:00:00+00:00", 5.0),
            _line("alpha", "2999-01-01T00:00:00+00:00", 1.0),
            _line("beta", "2000-01-01T00:00:00+00:00", 5.0),
            _line("beta", "2999-01-01T00:00:00+00:00", 1.0)]
    (paths.logs / "events.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    # 30-day window keeps only the far-future entry, not the year-2000 one
    assert window_spend(paths.home, paths.logs, "alpha", window="30d") == 1.0
    # a separate agent on an "all" window counts both
    assert window_spend(paths.home, paths.logs, "beta", window="all") == 6.0


def test_rebuild_after_rotation_keeps_archived_spend(tmp_path: Path) -> None:
    """Rotation self-heal: spend recorded before a log rotation must still count
    after it (the rebuild reads archives + active, not just the active file)."""
    paths = init_runtime(tmp_path)
    (paths.logs / "events.jsonl").write_text("", encoding="utf-8")
    _seed(paths.logs, "alpha", 0.40)
    assert window_spend(paths.home, paths.logs, "alpha", window="all") == 0.40

    # rotate: move the active log into a dated archive, start a fresh smaller active
    active = paths.logs / "events.jsonl"
    (paths.logs / "events-2020-01-01.jsonl").write_text(active.read_text(), encoding="utf-8")
    active.write_text("", encoding="utf-8")  # smaller than the recorded offset → forces rebuild

    # the archived 0.40 must survive the rebuild
    assert window_spend(paths.home, paths.logs, "alpha", window="all") == 0.40


def test_incremental_tail_does_not_reread_consumed_bytes(tmp_path: Path) -> None:
    """The ledger must fold only bytes appended past its offset (not re-scan the
    whole file each call). We corrupt an already-consumed line *other than the
    first* (so the head signature is unchanged and rotation-detection doesn't
    fire), keeping its byte length, then append a fresh event: an incremental
    tail keeps the cached prior spend; a full re-read would lose the corrupted
    line's spend."""
    paths = init_runtime(tmp_path)
    active = paths.logs / "events.jsonl"
    active.write_text("", encoding="utf-8")
    _seed(paths.logs, "alpha", 0.10)   # line 1 (the head — must stay intact)
    _seed(paths.logs, "alpha", 0.05)   # line 2 (will be corrupted post-consumption)
    assert window_spend(paths.home, paths.logs, "alpha", window="all") == 0.15

    lines = active.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[1] = "x" * (len(lines[1]) - 1) + "\n"   # garble line 2, same length, head unchanged
    active.write_text("".join(lines), encoding="utf-8")
    _seed(paths.logs, "alpha", 0.25)              # appended past the offset

    # cached 0.15 + new 0.25 = 0.40; a full re-read would drop the corrupted
    # 0.05 line and yield only 0.35
    assert window_spend(paths.home, paths.logs, "alpha", window="all") == 0.40


def test_rotation_then_growth_does_not_lose_spend(tmp_path: Path) -> None:
    """The real-world rotation bug: after rotation the new active grows PAST the
    old recorded offset before the next ledger read. A size-only check misses
    this (size > offset) and seeks into the wrong file; the head-signature check
    catches it. Ledger must equal the full-scan truth."""
    paths = init_runtime(tmp_path)
    active = paths.logs / "events.jsonl"
    active.write_text("", encoding="utf-8")
    _seed(paths.logs, "a", 3.00)
    assert window_spend(paths.home, paths.logs, "a", window="all") == 3.00

    (paths.logs / "events-2020-01-01.jsonl").write_text(active.read_text(), encoding="utf-8")
    active.write_text("", encoding="utf-8")
    _seed(paths.logs, "a", 2.50)
    _seed(paths.logs, "a", 2.50)   # new active now larger than the pre-rotation offset
    assert window_spend(paths.home, paths.logs, "a", window="all") == agent_spend(paths.logs, "a") == 8.00


def test_distinct_agent_ids_do_not_share_a_ledger_file(tmp_path: Path) -> None:
    """Ids that sanitize to the same filename (e.g. 'a/b' and 'a_b') must not
    collide onto one ledger — that would let one agent mask another's spend."""
    from jigga.runtime.spend_ledger import _ledger_path
    assert _ledger_path(tmp_path, "a/b") != _ledger_path(tmp_path, "a_b")


def test_malformed_cost_does_not_crash_or_overcount(tmp_path: Path) -> None:
    """A corrupt cost_usd (string / NaN / inf) must coerce to 0, not crash
    enforcement (window_spend is called on every model call)."""
    paths = init_runtime(tmp_path)
    (paths.logs / "events.jsonl").write_text("", encoding="utf-8")
    _seed(paths.logs, "a", 1.0)
    from jigga.runtime.audit import append_event
    for bad in ("oops", float("nan"), float("inf"), [1, 2]):
        append_event(paths.logs, "model.call", agent_id="a", provider="p",
                     model="m", dry_run=True, cost_usd=bad)
    assert window_spend(paths.home, paths.logs, "a", window="all") == 1.0


def test_corrupt_ledger_file_self_heals(tmp_path: Path) -> None:
    from jigga.runtime.spend_ledger import _ledger_path
    paths = init_runtime(tmp_path)
    (paths.logs / "events.jsonl").write_text("", encoding="utf-8")
    _seed(paths.logs, "a", 2.0)
    assert window_spend(paths.home, paths.logs, "a", window="all") == 2.0
    for junk in ("{ not json", '{"entries": "notalist"}', '{"offset": -5, "entries": []}', "[]"):
        _ledger_path(paths.home, "a").write_text(junk, encoding="utf-8")
        # rebuilds from the audit log rather than crashing or under-counting
        assert window_spend(paths.home, paths.logs, "a", window="all") == 2.0

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

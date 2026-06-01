"""Cron-field robustness: a malformed schedule must make the agent simply never
wake — never raise. _cron_due runs at the top of every supervisor tick, so an
unparseable cron on one agent would otherwise crash the whole heartbeat."""

from __future__ import annotations

from datetime import datetime

import pytest

from jigga.runtime.scheduler import _cron_due


@pytest.mark.parametrize("cron", [
    "not a cron at all", "x * * * *", "*/0 * * * *", "1-x * * * *",
    "* * * *", "", "60 * * * *",
])
def test_malformed_cron_never_raises_and_does_not_fire(cron: str) -> None:
    assert _cron_due(cron, datetime(2026, 1, 1, 9, 0)) is False


def test_valid_cron_still_fires() -> None:
    assert _cron_due("0 9 * * *", datetime(2026, 1, 1, 9, 0)) is True
    assert _cron_due("*/15 * * * *", datetime(2026, 1, 1, 9, 0)) is True
    assert _cron_due("0 9 * * *", datetime(2026, 1, 1, 10, 0)) is False

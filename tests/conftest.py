"""Pytest configuration for JIGGA tests.

Auto-enables notification dry-run mode for the whole test session so unit
tests never actually pop a desktop notification while exercising the
`notifications.send` capability against a real workflow run.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _disable_real_notifications_in_tests() -> None:
    # Tests can still exercise the real send_notification path explicitly by
    # passing dry_run=False; this fixture only changes the *default* the
    # dispatcher uses when reading the runtime config.
    os.environ["JIGGA_NOTIFICATION_MODE"] = "dry_run"
    yield
    os.environ.pop("JIGGA_NOTIFICATION_MODE", None)

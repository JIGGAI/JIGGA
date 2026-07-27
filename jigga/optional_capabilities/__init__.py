"""First-party optional capability registry.

The "bundled" capabilities in `jigga/runtime/capabilities.py::BUILTIN_CAPABILITY_DATA`
are always available — no setup required, no opt-in. This module is the
*other* tier: first-party capabilities that ship with JIGGA but only become
active when the user explicitly installs them (`jigga capabilities install
<name>`).

The motivating UX gap: shipping Google Calendar as bundled would force every
user to deal with Google Cloud Console / OAuth setup whether or not they
want calendar integration. By making it an opt-in install, users who don't
care never see it; users who do get a guided setup wizard.

Each entry in `REGISTRY` describes:
  - the capability `name` (used as the directory name under
    `~/.jigga/capabilities/<name>/` after install)
  - a short `summary` for the install menu
  - the source `manifest_path` shipped with JIGGA
  - an optional `setup_fn(paths) -> int` callable that runs interactively
    after the manifest is copied (e.g. OAuth setup). Return 0 on success,
    nonzero to roll back the install.

Adding a new first-party optional capability: drop the package under
`jigga/optional_capabilities/<your_name>/`, expose `setup(paths)` from its
`__init__.py`, ship a `manifest.yaml` alongside, then add one entry to
`REGISTRY` below.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jigga.optional_capabilities.email import setup as email_setup
from jigga.optional_capabilities.gog import setup as gog_setup
from jigga.optional_capabilities.google_calendar import (
    setup as google_calendar_setup,
)
from jigga.optional_capabilities.telegram import setup as telegram_setup


@dataclass(frozen=True)
class OptionalCapability:
    name: str
    summary: str
    manifest_path: Path
    setup_fn: Callable[..., int] | None = None


def _here() -> Path:
    return Path(__file__).resolve().parent


REGISTRY: dict[str, OptionalCapability] = {
    "email-imap": OptionalCapability(
        name="email-imap",
        summary="Provider-agnostic email — IMAP search/read, local drafts, SMTP send",
        manifest_path=_here() / "email" / "manifest.yaml",
        setup_fn=email_setup,
    ),
    "gog": OptionalCapability(
        name="gog",
        summary="Google Workspace (Gmail, Calendar, Drive, Sheets) via the gogcli tool",
        manifest_path=_here() / "gog" / "manifest.yaml",
        setup_fn=gog_setup,
    ),
    "google-calendar": OptionalCapability(
        name="google-calendar",
        summary="Read events from your Google Calendar via OAuth (native, no external tool)",
        manifest_path=_here() / "google_calendar" / "manifest.yaml",
        setup_fn=google_calendar_setup,
    ),
    "telegram": OptionalCapability(
        name="telegram",
        summary="Telegram channel — receive and reply to messages via a bot",
        manifest_path=_here() / "telegram" / "manifest.yaml",
        setup_fn=telegram_setup,
    ),
}


def list_available() -> list[OptionalCapability]:
    return list(REGISTRY.values())


def get_optional(name: str) -> OptionalCapability | None:
    return REGISTRY.get(name)

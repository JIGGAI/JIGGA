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

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable



@dataclass(frozen=True)
class OptionalCapability:
    name: str
    summary: str
    manifest_path: Path
    # Either a callable, or a `module.path:function` reference resolved on
    # first use. References are the norm: importing every connector's setup
    # eagerly cost ~33ms on *every* `jigga` invocation — including the ones
    # that never install anything — because one of them pulls in imaplib.
    setup_fn: Callable[..., int] | str | None = None

    def run_setup(self, paths: Any, **kwargs: Any) -> int:
        """Resolve (if needed) and run this capability's setup step."""
        if self.setup_fn is None:
            return 0
        target = self.setup_fn
        if isinstance(target, str):
            module_name, _, function_name = target.partition(":")
            target = getattr(importlib.import_module(module_name), function_name)
        return target(paths, **kwargs)


def _here() -> Path:
    return Path(__file__).resolve().parent


REGISTRY: dict[str, OptionalCapability] = {
    "daily-brief": OptionalCapability(
        name="daily-brief",
        summary="Skill — write the morning brief: what's ahead, what's open, what needs a decision",
        manifest_path=_here() / "daily_brief" / "manifest.yaml",
        setup_fn=None,   # a skill is instructions, not a connector: nothing to authenticate
    ),
    "image-generation": OptionalCapability(
        name="image-generation",
        summary="Generate images from prompts — Gemini (nano-banana) or an OpenAI-compatible endpoint",
        manifest_path=_here() / "image_generation" / "manifest.yaml",
        setup_fn="jigga.optional_capabilities.image_generation:setup",
    ),
    "brave-search": OptionalCapability(
        name="brave-search",
        summary="Web search via the Brave Search API (API key, free tier)",
        manifest_path=_here() / "brave_search" / "manifest.yaml",
        setup_fn="jigga.optional_capabilities.brave_search:setup",
    ),
    "searxng": OptionalCapability(
        name="searxng",
        summary="Web search via a SearXNG metasearch instance (no API key)",
        manifest_path=_here() / "searxng" / "manifest.yaml",
        setup_fn="jigga.optional_capabilities.searxng:setup",
    ),
    "email-imap": OptionalCapability(
        name="email-imap",
        summary="Provider-agnostic email — IMAP search/read, local drafts, SMTP send",
        manifest_path=_here() / "email" / "manifest.yaml",
        setup_fn="jigga.optional_capabilities.email:setup",
    ),
    "gog": OptionalCapability(
        name="gog",
        summary="Google Workspace (Gmail, Calendar, Drive, Sheets) via the gogcli tool",
        manifest_path=_here() / "gog" / "manifest.yaml",
        setup_fn="jigga.optional_capabilities.gog:setup",
    ),
    "google-calendar": OptionalCapability(
        name="google-calendar",
        summary="Read events from your Google Calendar via OAuth (native, no external tool)",
        manifest_path=_here() / "google_calendar" / "manifest.yaml",
        setup_fn="jigga.optional_capabilities.google_calendar:setup",
    ),
    "telegram": OptionalCapability(
        name="telegram",
        summary="Telegram channel — receive and reply to messages via a bot",
        manifest_path=_here() / "telegram" / "manifest.yaml",
        setup_fn="jigga.optional_capabilities.telegram:setup",
    ),
}


def list_available() -> list[OptionalCapability]:
    return list(REGISTRY.values())


def get_optional(name: str) -> OptionalCapability | None:
    return REGISTRY.get(name)

"""Google Calendar first-party optional capability.

Provides `setup(paths, *, input_fn=input, print_fn=print, oauth_runner=None)`
which is invoked by `jigga capabilities install google-calendar` after the
manifest is copied into the runtime. Walks the user through obtaining
Google Cloud Console OAuth credentials, copies them into the secrets dir,
runs the loopback PKCE OAuth flow, and verifies the connection with one
`events.list` call.

The function is parameterised on its I/O (input_fn/print_fn) and the OAuth
runner so tests can drive it deterministically without prompting or hitting
the real Google endpoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from jigga.runtime.google_calendar import (
    client_config_path,
    list_events,
    run_oauth_flow,
    store_client_config,
)


_INSTRUCTIONS = """
JIGGA needs an OAuth client to talk to Google Calendar on your behalf.

If you don't already have one:
  1. Open https://console.cloud.google.com/apis/credentials
  2. Create (or select) a Google Cloud project.
  3. Enable the Google Calendar API for that project:
       https://console.cloud.google.com/apis/library/calendar-json.googleapis.com
  4. Create credentials → OAuth 2.0 Client ID → Application type: Desktop app.
  5. Download the JSON file Google gives you.
"""


def setup(
    paths,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
    oauth_runner: Callable[..., object] | None = None,
) -> int:
    """Interactive Google Calendar OAuth setup. Returns 0 on success."""
    print_fn("\n=== Google Calendar setup ===")
    print_fn(_INSTRUCTIONS)

    while True:
        raw = input_fn("Path to your OAuth client JSON (or 'q' to abort): ").strip()
        if raw.lower() in {"q", "quit", "exit"}:
            print_fn("Aborted. Re-run `jigga capabilities install google-calendar` when ready.")
            return 1
        if not raw:
            continue
        source = Path(raw).expanduser()
        if not source.is_file():
            print_fn(f"  File not found: {source}. Try again.")
            continue
        try:
            store_client_config(paths.secrets, source)
            break
        except (ValueError, OSError) as exc:
            print_fn(f"  Could not use that file: {exc}")
            continue

    print_fn(f"  Client config installed at {client_config_path(paths.secrets)}")
    print_fn("\nLaunching OAuth flow — your browser should open shortly.")
    print_fn("(If it doesn't, look for the URL printed in JIGGA logs and open it manually.)")

    runner = oauth_runner or run_oauth_flow
    try:
        tokens = runner(paths.secrets)
    except (RuntimeError, FileNotFoundError) as exc:
        print_fn(f"\nLogin failed: {exc}")
        return 1

    # Verify with one read call. Don't fail the install if verification errors
    # — tokens are valid; the user might have an empty calendar or a transient
    # API hiccup. Just surface the result for confidence.
    try:
        events = list_events(tokens, max_results=1)
        upcoming = len(events.get("items") or [])
        print_fn(f"\nConnected. Found {upcoming} upcoming event(s) in your primary calendar.")
    except RuntimeError as exc:  # pragma: no cover - depends on real API state
        print_fn(f"\nLogin succeeded but verification call errored: {exc}")
        print_fn("Capability is installed; try `jigga calendar status` to inspect.")

    return 0

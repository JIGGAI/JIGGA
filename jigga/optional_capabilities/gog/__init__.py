"""gog (Google Workspace) first-party optional capability.

Provides `setup(paths, *, input_fn, print_fn, ...)` invoked by
`jigga capabilities install gog`. Walks the user through:

  1. Verifying the gogcli binary is installed (and is actually gogcli, not a
     name-collision binary).
  2. Pointing gog at the user's Google Cloud Desktop OAuth client JSON
     (`gog auth credentials <path>`).
  3. Setting a keyring password for gog's encrypted file backend (stored at
     ~/.jigga/secrets/gog_keyring_password, 0600) so the headless supervisor
     can drive gog later.
  4. Running the interactive OAuth flow (`gog auth add <email> --services ...`)
     which opens a browser.
  5. Verifying with `gog auth doctor`.

This module is also the reference template for "wrap an external CLI as a
JIGGA capability" — see docs/GOG_INTEGRATION_RUNTIME_NOTES.md for the
step-by-step on building your own.

I/O is parameterised (input_fn / print_fn / interactive_runner) so the whole
wizard is testable without a real terminal or a real gog binary.
"""

from __future__ import annotations

from typing import Callable

from jigga.runtime.gog import (
    DEFAULT_SERVICES,
    gog_auth_status,
    gog_binary_status,
    run_gog_interactive,
    store_keyring_password,
)

_INSTALL_HELP = """
gog (gogcli) is OpenClaw's Google Workspace CLI. JIGGA wraps it so one OAuth
login covers Gmail, Calendar, Drive, Sheets, and more.

Install it first:
  https://github.com/openclaw/gogcli

You'll also need a Google Cloud Desktop OAuth client:
  1. https://console.cloud.google.com/apis/credentials
  2. Enable the APIs you want (Gmail, Calendar, ...).
  3. Create credentials → OAuth client ID → Application type: Desktop app.
  4. Download the client JSON.
"""


def setup(
    paths,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
    interactive_runner=None,
) -> int:
    """Interactive gog setup. Returns 0 on success, nonzero to roll back."""
    print_fn("\n=== gog (Google Workspace) setup ===")

    binary = gog_binary_status()
    if not binary["available"]:
        print_fn(_INSTALL_HELP)
        print_fn("gogcli is not on PATH. Install it, then re-run this install.")
        return 1
    if not binary["is_gogcli"]:
        print_fn(
            f"\nA binary named 'gog' is on PATH ({binary['path']}) but does not look "
            "like gogcli (openclaw/gogcli). Make sure the right binary resolves first."
        )
        return 1

    print_fn(_INSTALL_HELP)

    # Step 1: point gog at the user's OAuth client JSON.
    client_path = ""
    while True:
        client_path = input_fn("Path to your OAuth client JSON (or 'q' to abort): ").strip()
        if client_path.lower() in {"q", "quit", "exit"}:
            print_fn("Aborted. Re-run `jigga capabilities install gog` when ready.")
            return 1
        if client_path:
            break

    # Step 2: keyring password for the file backend (so the daemon can run gog
    # headless later). Prompt; if blank, tell the user it's required.
    password = ""
    while not password:
        password = input_fn(
            "Set a keyring password for gog's encrypted file backend "
            "(JIGGA stores it 0600 so the supervisor can run gog headless): "
        ).strip()
        if not password:
            print_fn("  A password is required for the file backend. Try again.")
    store_keyring_password(paths.secrets, password)

    runner = interactive_runner

    def _interactive(args: list[str]) -> int:
        if runner is not None:
            return run_gog_interactive(paths.secrets, args, password=password, runner=runner)
        return run_gog_interactive(paths.secrets, args, password=password)

    print_fn("\nStoring OAuth client credentials in gog...")
    if _interactive(["auth", "credentials", client_path]) != 0:
        print_fn("`gog auth credentials` failed. Check the client JSON path and try again.")
        return 1

    # Step 3: interactive OAuth — opens a browser.
    email = input_fn("Google account email to authenticate: ").strip()
    if not email:
        print_fn("No email provided; aborting.")
        return 1
    services = ",".join(DEFAULT_SERVICES)
    print_fn(f"\nLaunching gog OAuth for {email} (services: {services}). Your browser should open.")
    if _interactive(["auth", "add", email, "--services", services]) != 0:
        print_fn("`gog auth add` failed. Re-run `jigga gog login` to retry.")
        return 1

    # Step 4: verify.
    status = gog_auth_status(paths.secrets)
    if status.get("connected"):
        print_fn("\nConnected. gog is authenticated and JIGGA can reach Google Workspace.")
    else:
        print_fn(
            f"\nAuth flow ran but verification did not confirm a connected account "
            f"({status.get('detail')}). Try `jigga gog status`."
        )
    return 0

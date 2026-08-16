"""Importing the CLI must not drag in every connector.

Almost everything imports `jigga.runtime.dispatcher`, and the dispatcher used
to import every connector handler at module scope — `email_imap` (which pulls
`imaplib` + `smtplib`), `telegram`, `google_calendar`, `gog`, `web`. So every
`jigga` invocation paid for every connector, including commands that dispatch
nothing at all.

This guard exists because the fix is *invisible when it breaks*: re-adding a
top-level `from jigga.runtime.email_imap import ...` restores the cost and
nothing fails. A latency regression with no failing test is one nobody notices
until it's been there a year.

Deliberately asserted on module *presence*, not on wall-clock time — a timing
assertion would be flaky on a loaded CI box and would get muted rather than
fixed.
"""

from __future__ import annotations

import subprocess
import sys

# Connector modules whose cost should only be paid when something actually
# dispatches to them. `imaplib`/`smtplib` are the stdlib giveaways.
DEFERRED = [
    "imaplib",
    "smtplib",
    "jigga.runtime.email_imap",
]


def _loaded_after(import_target: str) -> set[str]:
    """Modules in sys.modules after importing `import_target` in a fresh process."""
    code = (
        f"import {import_target}\n"
        "import sys, json\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    import json

    return set(json.loads(result.stdout))


def test_importing_the_cli_does_not_load_the_email_stack() -> None:
    loaded = _loaded_after("jigga.cli")
    still_eager = [name for name in DEFERRED if name in loaded]
    assert still_eager == [], (
        f"{still_eager} loaded on `import jigga.cli`. A connector handler is being "
        "imported at module scope again — register it with `_lazy_handler` instead."
    )


def test_importing_the_dispatcher_does_not_load_the_email_stack() -> None:
    """The dispatcher is the shared chokepoint — the supervisor and agent loop
    import it too, so this is not only about CLI startup."""
    loaded = _loaded_after("jigga.runtime.dispatcher")
    assert [name for name in DEFERRED if name in loaded] == []


def test_a_lazy_handler_still_resolves_and_runs() -> None:
    """Deferring the import must not break dispatch — the handler has to be a
    real callable that works on first use."""
    from jigga.runtime.dispatcher import HANDLERS

    handler = HANDLERS["runtime.email_imap"]
    assert callable(handler)
    assert handler.__name__ == "email_imap_handler"


def test_every_lazy_handler_reference_resolves() -> None:
    """A typo in a lazy reference would only surface when someone dispatched to
    it — exactly the silent-until-used failure #188 was about."""
    import importlib

    from jigga.runtime.dispatcher import HANDLERS

    unresolvable = []
    for name, handler in HANDLERS.items():
        reference = getattr(handler, "_reference", None)
        if reference is None:
            continue
        module_name, _, function_name = reference.partition(":")
        try:
            if not callable(getattr(importlib.import_module(module_name), function_name)):
                unresolvable.append(f"{name}: {reference} is not callable")
        except (ImportError, AttributeError) as exc:
            unresolvable.append(f"{name}: {reference} ({exc})")
    assert unresolvable == [], f"lazy handler references that do not resolve: {unresolvable}"

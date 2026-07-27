"""Provider-agnostic email (IMAP/SMTP) first-party optional capability.

`setup(paths, ...)` is invoked by `jigga capabilities install email-imap`. It
collects the IMAP/SMTP hosts and a username + app-password, verifies nothing
over the network (local-first; the first search surfaces auth problems with a
clear error), and stores credentials at `~/.jigga/secrets/email_imap.json`
(0600). I/O is parameterised (input_fn / print_fn) so the wizard is testable.

BYO credentials: use an app-specific password where the provider offers one —
never your main account password.
"""

from __future__ import annotations

from typing import Callable

from jigga.runtime.email_imap import store_credentials

_HELP = """
Connect any IMAP/SMTP mailbox (Fastmail, iCloud, Outlook, self-hosted, ...).

  You'll need: the IMAP host, the SMTP host, your username, and an
  app-specific password (create one in your provider's security settings —
  don't use your main account password).

  Common hosts — Fastmail: imap.fastmail.com / smtp.fastmail.com
                 iCloud:   imap.mail.me.com / smtp.mail.me.com
"""


def setup(
    paths,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
) -> int:
    """Interactive email setup. Returns 0 on success, 1 on abort."""
    print_fn("\n=== Email (IMAP/SMTP) setup ===")
    print_fn(_HELP)

    imap_host = input_fn("IMAP host: ").strip()
    smtp_host = input_fn("SMTP host: ").strip()
    username = input_fn("Username (usually your email address): ").strip()
    password = input_fn("App password: ").strip()
    if not (imap_host and smtp_host and username and password):
        print_fn("All of IMAP host, SMTP host, username, and password are required — aborting.")
        return 1
    imap_port = input_fn("IMAP port [993]: ").strip() or "993"
    smtp_port = input_fn("SMTP port [465]: ").strip() or "465"
    security = (input_fn("SMTP security ssl/starttls [ssl]: ").strip() or "ssl").lower()
    from_address = input_fn(f"From address [{username}]: ").strip() or username

    path = store_credentials(paths.home, {
        "imap_host": imap_host, "imap_port": int(imap_port),
        "smtp_host": smtp_host, "smtp_port": int(smtp_port),
        "smtp_security": security if security in ("ssl", "starttls") else "ssl",
        "username": username, "password": password, "from_address": from_address,
    })
    print_fn(f"Credentials stored at {path} (0600).")
    print_fn("Try it: give an agent the email.search tool, or run a workflow step "
             "with action email.search and input {filters: [unread]}.")
    return 0

"""gogcli (`gog`) wrapper — unified Google Workspace via OpenClaw's gog CLI.

Instead of reimplementing each Google service natively, JIGGA shells out to
`gog` (https://github.com/openclaw/gogcli) — a single binary that the user
authenticates once (`gog auth add you@gmail.com --services gmail,calendar,...`)
and that then exposes Gmail, Calendar, Drive, Sheets, Docs, Tasks, and more.
This mirrors how OpenClaw integrates Google.

## Why this is the design

- One OAuth flow, one credential set, every Google service. The user creates
  their own Google Cloud Desktop OAuth client (bring-your-own, consistent with
  JIGGA's local-first stance — see docs/GOOGLE_CALENDAR_RUNTIME_NOTES.md).
- gog stores tokens in a keyring. For JIGGA's daemon/headless use we select
  gog's **encrypted file backend** (`GOG_KEYRING_BACKEND=file`) and inject
  `GOG_KEYRING_PASSWORD` so the supervisor can drive gog without a desktop
  session bus.

## Subprocess routing (two halves, per the runtime.sandbox rule)

- **Interactive auth** (`gog auth credentials`, `gog auth add` — opens a
  browser) is render-side: run directly with the user's full environment +
  TTY. Lives in the optional-capability setup wizard / `jigga gog login`.
- **Action calls** (`gog gmail search`, `gog calendar events` — run by the
  supervisor, possibly headless) are authority-side: routed through
  `sandbox.run_sandboxed` with a restricted env, plus the keyring backend +
  password injected via `SandboxSpec.extra_env`.

## Name-collision guard

There is at least one unrelated tool also named `gog` on some systems (a
node-based script runner). `gog_binary_status()` verifies the binary on PATH
actually behaves like gogcli (`gog auth doctor`) rather than trusting the name.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir
from jigga.core.models import WorkflowStep
from jigga.runtime.capabilities import CapabilityManifest
from jigga.runtime.sandbox import SandboxSpec, run_sandboxed

GOG_BINARY = "gog"
KEYRING_PASSWORD_FILENAME = "gog_keyring_password"
DEFAULT_SERVICES = ("gmail", "calendar", "drive", "sheets", "docs")
DEFAULT_TIMEOUT_SECONDS = 60.0

# Action → gog argv template. Each entry is the subcommand path; per-action
# argument building is handled in the dispatch functions below so we can map
# workflow-step input to gog flags explicitly (rather than blindly forwarding).
SUPPORTED_ACTIONS = (
    "gog.gmail_search",
    "gog.gmail_get",
    "gog.gmail_draft",
    "gog.gmail_send",
    "gog.calendar_events",
    "gog.calendar_create",
    "gog.drive_list",
    "gog.drive_get",
    "gog.drive_share",
    "gog.sheets_get",
    "gog.sheets_append",
    "gog.docs_get",
    "gog.docs_write",
)

# Sending mail is the one destructive Google action we expose. It's gated:
# the handler refuses unless the step input explicitly sets `confirm_send: true`
# AND the capability/agent permission flow has allowed it. Drafts are always
# safe and preferred.
SEND_ACTION = "gog.gmail_send"


# --- Keyring password storage ----------------------------------------------


def keyring_password_path(secrets_dir: Path) -> Path:
    return secrets_dir / KEYRING_PASSWORD_FILENAME


def store_keyring_password(secrets_dir: Path, password: str) -> Path:
    ensure_dir(secrets_dir)
    path = keyring_password_path(secrets_dir)
    path.write_text(password, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass
    return path


def load_keyring_password(secrets_dir: Path) -> str | None:
    path = keyring_password_path(secrets_dir)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def _keyring_env(secrets_dir: Path) -> dict[str, str]:
    """Env pairs that make gog use its encrypted file backend with JIGGA's
    stored password. Returned for injection via SandboxSpec.extra_env."""
    env = {"GOG_KEYRING_BACKEND": "file"}
    password = load_keyring_password(secrets_dir)
    if password:
        env["GOG_KEYRING_PASSWORD"] = password
    return env


def run_gog_interactive(
    secrets_dir: Path,
    args: list[str],
    *,
    password: str | None = None,
    runner=subprocess.run,
) -> int:
    """Run an interactive gog command (auth credentials / auth add) attached to
    the user's terminal so browser + prompts work.

    Render-side per the routing rule: full user environment (browser, display,
    locale) plus the keyring backend + password merged in. NOT sandboxed —
    these commands need the session env the sandbox strips. `runner` is
    injectable for tests.
    """
    env = dict(os.environ)
    env["GOG_KEYRING_BACKEND"] = "file"
    pw = password or load_keyring_password(secrets_dir)
    if pw:
        env["GOG_KEYRING_PASSWORD"] = pw
    completed = runner([GOG_BINARY, *args], env=env, check=False)
    return completed.returncode


# --- Binary detection / auth status ----------------------------------------


# Distinctive gogcli markers. Deliberately excludes words we pass as argv
# ("auth", "services", "doctor") — a name-collision binary will echo our own
# arguments back, so matching on those produces false positives. These tokens
# are things gogcli prints but a dumb script-runner won't.
GOGCLI_MARKERS = ("gmail", "calendar", "drive", "keyring", "credential", "workspace", "sheets")


def gog_binary_status() -> dict[str, Any]:
    """Report whether a usable gogcli binary is on PATH.

    Distinguishes 'no gog at all' from 'a gog that isn't gogcli' (name
    collision — e.g. an unrelated node-based `gog` script runner). Probes with
    `gog auth services`, which on real gogcli lists Google Workspace services,
    and requires at least two distinctive markers. We do NOT trust the name.
    """
    path = shutil.which(GOG_BINARY)
    if path is None:
        return {"available": False, "path": None, "is_gogcli": False, "reason": "gog not on PATH"}
    try:
        completed = subprocess.run(
            [GOG_BINARY, "auth", "services"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": True, "path": path, "is_gogcli": False, "reason": f"probe failed: {exc}"}
    blob = f"{completed.stdout}\n{completed.stderr}".lower()
    hits = sum(1 for token in GOGCLI_MARKERS if token in blob)
    looks_like_gogcli = hits >= 2
    return {
        "available": True,
        "path": path,
        "is_gogcli": looks_like_gogcli,
        "reason": None if looks_like_gogcli else "binary named 'gog' does not look like gogcli (openclaw/gogcli)",
    }


def gog_auth_status(secrets_dir: Path) -> dict[str, Any]:
    """Non-interactive auth check via `gog auth doctor --check --no-input`,
    run with the keyring backend + password injected (the same way action
    calls run) so the result reflects what the supervisor would actually see."""
    binary = gog_binary_status()
    if not binary["available"] or not binary["is_gogcli"]:
        return {"connected": False, "binary": binary, "detail": binary.get("reason")}
    spec = SandboxSpec(
        command=GOG_BINARY,
        args=["auth", "doctor", "--check", "--no-input"],
        secrets_required=[],
        timeout_seconds=20.0,
        extra_env=_keyring_env(secrets_dir),
    )
    try:
        completed = run_sandboxed(spec)
    except subprocess.TimeoutExpired:
        return {"connected": False, "binary": binary, "detail": "gog auth doctor timed out"}
    return {
        "connected": completed.returncode == 0,
        "binary": binary,
        "returncode": completed.returncode,
        "detail": (completed.stderr or completed.stdout or "").strip()[:500] or None,
    }


# --- Action dispatch -------------------------------------------------------


def _run_gog_json(secrets_dir: Path, args: list[str]) -> dict[str, Any]:
    """Run a gog subcommand with --json, sandboxed, and parse stdout.

    The global `--json` flag is placed first (gogcli accepts global flags
    before the subcommand). Returns the parsed JSON, or raises RuntimeError
    with gog's stderr on failure / unparseable output.
    """
    spec = SandboxSpec(
        command=GOG_BINARY,
        args=["--json", *args],
        secrets_required=[],
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        extra_env=_keyring_env(secrets_dir),
    )
    completed = run_sandboxed(spec)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[:800]
        raise RuntimeError(f"gog {' '.join(args)} failed (exit {completed.returncode}): {detail}")
    stdout = completed.stdout.strip()
    if not stdout:
        return {}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gog returned non-JSON stdout: {stdout[:300]}") from exc


def _not_installed(action: str, binary: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "capability.gog",
        "action": action,
        "status": "gog.not_installed" if not binary["available"] else "gog.not_gogcli",
        "message": (
            "gogcli is not installed. Install it (https://github.com/openclaw/gogcli) "
            "and run `jigga capabilities install gog`."
            if not binary["available"]
            else "A binary named 'gog' is on PATH but does not look like gogcli. "
            "Ensure the openclaw/gogcli binary is the one resolved first."
        ),
    }


def _not_connected(action: str) -> dict[str, Any]:
    return {
        "source": "capability.gog",
        "action": action,
        "status": "gog.not_connected",
        "message": "gog has no authenticated account. Run `jigga gog login`.",
    }


def gog_handler(
    step: WorkflowStep,
    _capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime,
) -> Any:
    """Dispatch a workflow step to the matching gog subcommand."""
    secrets_dir = runtime.home / "secrets"
    binary = gog_binary_status()
    if not binary["available"] or not binary["is_gogcli"]:
        return _not_installed(step.action, binary)

    params = resolved_input if isinstance(resolved_input, dict) else {}

    if step.action == "gog.gmail_search":
        return _gmail_search(secrets_dir, step.action, params)
    if step.action == "gog.gmail_get":
        return _gmail_get(secrets_dir, step.action, params)
    if step.action == "gog.gmail_draft":
        return _gmail_draft(secrets_dir, step.action, params)
    if step.action == "gog.gmail_send":
        return _gmail_send(secrets_dir, step.action, params)
    if step.action == "gog.calendar_events":
        return _calendar_events(secrets_dir, step.action, params)
    if step.action == "gog.calendar_create":
        return _calendar_create(secrets_dir, step.action, params)
    if step.action == "gog.drive_list":
        return _drive_list(secrets_dir, step.action, params)
    if step.action == "gog.drive_get":
        return _drive_get(secrets_dir, step.action, params)
    if step.action == "gog.drive_share":
        return _drive_share(secrets_dir, step.action, params)
    if step.action == "gog.sheets_get":
        return _sheets_get(secrets_dir, step.action, params)
    if step.action == "gog.sheets_append":
        return _sheets_append(secrets_dir, step.action, params)
    if step.action == "gog.docs_get":
        return _docs_get(secrets_dir, step.action, params)
    if step.action == "gog.docs_write":
        return _docs_write(secrets_dir, step.action, params)
    raise ValueError(
        f"Unknown gog action: {step.action!r}. Supported: {', '.join(SUPPORTED_ACTIONS)}."
    )


def _result(action: str, data: Any) -> dict[str, Any]:
    return {"source": "capability.gog", "action": action, "status": "ok", "data": data}


def _gmail_search(secrets_dir: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    query = str(params.get("query") or "is:unread newer_than:1d")
    args = ["gmail", "search", query]
    max_results = params.get("max")
    if max_results is not None:
        args += ["--max", str(int(max_results))]
    return _result(action, _run_gog_json(secrets_dir, args))


def _gmail_get(secrets_dir: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    message_id = str(params.get("message_id") or "").strip()
    if not message_id:
        raise ValueError("gog.gmail_get requires 'message_id' in input")
    args = ["gmail", "get", message_id]
    if params.get("sanitize_content", True):
        args.append("--sanitize-content")
    return _result(action, _run_gog_json(secrets_dir, args))


def _gmail_draft(secrets_dir: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    to = str(params.get("to") or "").strip()
    subject = str(params.get("subject") or "").strip()
    if not to:
        raise ValueError("gog.gmail_draft requires 'to' in input")
    args = ["gmail", "drafts", "create", "--to", to]
    if subject:
        args += ["--subject", subject]
    return _result(action, _run_gog_json(secrets_dir, args))


def _gmail_send(secrets_dir: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Sending is the one destructive action. Refuse unless the step input
    explicitly opts in with confirm_send: true. The recommended path is to
    create a draft (gog.gmail_draft) and let the user send from Gmail; an
    autonomous send should be a deliberate, approval-gated workflow step."""
    if not bool(params.get("confirm_send", False)):
        return {
            "source": "capability.gog",
            "action": action,
            "status": "gog.send_refused",
            "message": (
                "gog.gmail_send requires confirm_send: true in the step input. "
                "Prefer gog.gmail_draft and send from Gmail, or gate this step "
                "with approval: required."
            ),
        }
    to = str(params.get("to") or "").strip()
    subject = str(params.get("subject") or "").strip()
    if not to:
        raise ValueError("gog.gmail_send requires 'to' in input")
    args = ["gmail", "send", "--to", to]
    if subject:
        args += ["--subject", subject]
    return _result(action, _run_gog_json(secrets_dir, args))


def _calendar_events(secrets_dir: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    args = ["calendar", "events"]
    if params.get("today", True):
        args.append("--today")
    return _result(action, _run_gog_json(secrets_dir, args))


def _calendar_create(secrets_dir: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    summary = str(params.get("summary") or "").strip()
    if not summary:
        raise ValueError("gog.calendar_create requires 'summary' in input")
    args = ["calendar", "create", "--summary", summary]
    if params.get("from"):
        args += ["--from", str(params["from"])]
    if params.get("to"):
        args += ["--to", str(params["to"])]
    if params.get("with_meet"):
        args.append("--with-meet")
    return _result(action, _run_gog_json(secrets_dir, args))


# --- Drive -----------------------------------------------------------------


def _drive_list(secrets_dir: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Browse a Drive folder tree. Defaults to the user's root folder; the
    Drive API accepts the alias 'root'. Pass folder_id to scope to a subtree."""
    folder_id = str(params.get("folder_id") or "root")
    args = ["drive", "tree", "--parent", folder_id]
    depth = params.get("depth")
    if depth is not None:
        args += ["--depth", str(int(depth))]
    return _result(action, _run_gog_json(secrets_dir, args))


def _drive_get(secrets_dir: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    file_id = str(params.get("file_id") or "").strip()
    if not file_id:
        raise ValueError("gog.drive_get requires 'file_id' in input")
    args = ["drive", "get", file_id]
    fields = params.get("fields")
    if fields:
        args += ["--fields", str(fields)]
    return _result(action, _run_gog_json(secrets_dir, args))


def _drive_share(secrets_dir: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Share a Drive file. This has EXTERNAL blast radius (grants someone else
    access), so it's gated like gmail_send: refuse unless confirm_share: true.
    Prefer gating the workflow step with approval: required instead of baking
    confirm_share into a stored workflow."""
    if not bool(params.get("confirm_share", False)):
        return {
            "source": "capability.gog",
            "action": action,
            "status": "gog.share_refused",
            "message": (
                "gog.drive_share grants external access and requires "
                "confirm_share: true in the step input. Prefer gating the step "
                "with approval: required."
            ),
        }
    file_id = str(params.get("file_id") or "").strip()
    email = str(params.get("email") or "").strip()
    if not file_id or not email:
        raise ValueError("gog.drive_share requires 'file_id' and 'email' in input")
    to = str(params.get("to") or "user")
    args = ["drive", "share", file_id, "--to", to, "--email", email]
    if params.get("notify"):
        args.append("--notify")
    return _result(action, _run_gog_json(secrets_dir, args))


# --- Sheets ----------------------------------------------------------------


def _sheets_get(secrets_dir: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    spreadsheet_id = str(params.get("spreadsheet_id") or "").strip()
    cell_range = str(params.get("range") or "").strip()
    if not spreadsheet_id or not cell_range:
        raise ValueError("gog.sheets_get requires 'spreadsheet_id' and 'range' in input")
    args = ["sheets", "get", spreadsheet_id, cell_range]
    return _result(action, _run_gog_json(secrets_dir, args))


def _sheets_append(secrets_dir: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Append a row to a sheets 'table'. gog takes the row as a pipe-delimited
    string (e.g. 'Ship README|done'). Accepts either a pre-joined 'row' string
    or a 'values' list which we join with '|'."""
    spreadsheet_id = str(params.get("spreadsheet_id") or "").strip()
    table = str(params.get("table") or "").strip()
    if not spreadsheet_id or not table:
        raise ValueError("gog.sheets_append requires 'spreadsheet_id' and 'table' in input")
    row = params.get("row")
    if row is None:
        values = params.get("values")
        if not isinstance(values, list) or not values:
            raise ValueError("gog.sheets_append requires 'row' (string) or 'values' (list) in input")
        row = "|".join(str(v) for v in values)
    args = ["sheets", "table", "append", spreadsheet_id, table, str(row)]
    return _result(action, _run_gog_json(secrets_dir, args))


# --- Docs ------------------------------------------------------------------


def _docs_get(secrets_dir: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    doc_id = str(params.get("doc_id") or "").strip()
    if not doc_id:
        raise ValueError("gog.docs_get requires 'doc_id' in input")
    args = ["docs", "raw", doc_id, "--pretty"]
    return _result(action, _run_gog_json(secrets_dir, args))


def _docs_write(secrets_dir: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    """Append markdown text to a doc the user owns. Writes to the user's own
    document (not external), so it's ungated here — gate with approval:
    required at the workflow level if you want a human check."""
    doc_id = str(params.get("doc_id") or "").strip()
    text = params.get("text")
    if not doc_id or not text:
        raise ValueError("gog.docs_write requires 'doc_id' and 'text' in input")
    args = ["docs", "write", doc_id, "--append", "--markdown", "--text", str(text)]
    return _result(action, _run_gog_json(secrets_dir, args))

# Notification Adapter Runtime Notes

First real connector implementation, replacing the dry-run `notifications.send` capability handler with a cross-platform desktop notification sender. Milestone A's first slice per `docs/ROADMAP_TO_PRODUCTION.md`.

## What changed

- New `jigga/runtime/notifications.py`:
  - `NotificationRequest` / `NotificationResult` dataclasses.
  - `send_notification(request, dry_run)` with platform dispatch.
  - `delivery_mode(home)` resolver: env override → config.yaml → default.
  - Platform handlers: `_send_linux` (notify-send), `_send_macos` (osascript), Windows reports `unsupported-windows` cleanly.
  - AppleScript-safe escaping for user-supplied titles/bodies.
- `dispatcher._notifications_handler` rewritten to call the real adapter, with structured input handling (`title`/`body`/`content`/`urgency`) and a coercer that pulls a `summary` field out of upstream dict outputs (matches what `summarize_day` produces).
- Audit events: `notification.delivered` (status `ok`) or `notification.failed` (status `error`) on every send, carrying `backend`, `dry_run`, `urgency`, `title`, and `error`.
- Bundled `notifications` capability bumped to v0.2.0, handler changed from `dry_run.notifications` → `runtime.notifications`, summary rewritten.
- `dry_run.notifications` handler kept in `HANDLERS` so capability packs that explicitly want the legacy stub can still pin to it.
- `jigga init` writes `notifications: {delivery_mode: real}` to `config.yaml`.
- `tests/conftest.py` autouse fixture sets `JIGGA_NOTIFICATION_MODE=dry_run` for the test session so pytest never pops desktop notifications.

## Platform notes

- **Linux:** requires `notify-send` from libnotify. Missing-tool case returns a graceful `NotificationResult(delivered=False, backend="linux", error="notify-send not installed (apt install libnotify-bin / pacman -S libnotify)")`.
- **macOS:** uses `osascript -e 'display notification "..." with title "..."'`. AppleScript quotes/backslashes/newlines escaped in user input. The macOS notification UI hides bodies past one line, so we collapse newlines into spaces.
- **Windows:** explicitly unsupported in this slice. Returns `backend="unsupported-windows"` with an error pointing future implementers at PowerShell + WinRT toasts. The handler shape is platform-agnostic — adding Windows is a one-function change.

## Why this handler does NOT go through `runtime.sandbox`

Desktop notifications inherit the user's locale, X display (`DISPLAY`/`WAYLAND_DISPLAY`), notification daemon socket, and audio device — all of which live in env vars that `sandbox.build_restricted_env` would strip. These are local-UX tools, not bounded external CLIs, so the env scrub buys nothing and breaks delivery. Documented in `notifications.py` module docstring.

## Test mode

Two ways to suppress real delivery:

1. **Env var:** `JIGGA_NOTIFICATION_MODE=dry_run` (highest precedence, wins over config). pytest sets this automatically.
2. **Config:** `notifications.delivery_mode: dry_run` in `~/.jigga/config.yaml`.

Both resolve via `notifications.delivery_mode(home)`. The handler reads this once per invocation and routes to `send_notification(req, dry_run=True)` which returns `NotificationResult(delivered=False, backend="dry_run")` without touching subprocess.

## Workflow step input shapes

The handler accepts both ergonomic and explicit shapes:

```yaml
# explicit
- id: notify
  action: notifications.send
  input:
    title: "Meeting reminder"
    body: "Standup at 9:30"
    urgency: high

# upstream-referenced body
- id: notify
  action: notifications.send
  input:
    content: day_summary  # references a prior step's output
```

When `content`/`body` is a dict (e.g. the `summarize_day` output `{"summary": "...", "input": {...}}`), the body coercer extracts the `summary` field. Other dicts are JSON-rendered. Lists join with newlines. Scalars are stringified.

## Follow-up work

- Windows implementation (PowerShell + WinRT toast templates).
- Quiet hours / urgency routing per `docs/tools/NOTIFICATION_ROUTER.md` (currently `urgency` is forwarded but not gated against quiet-hours config).
- Digest mode (`low` urgency notifications batched into a single delivery).
- Alternative channels (Slack DM, email, mobile push) via the same capability shape.
- Approval gate for "high"/"critical" urgency when permission_mode is `ask`.

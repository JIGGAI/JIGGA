# gog (Google Workspace) Integration Runtime Notes

Wraps [openclaw/gogcli](https://github.com/openclaw/gogcli) — "Google Workspace in your terminal" — as a JIGGA optional capability. One `gog auth` login covers Gmail, Calendar, Drive, Sheets, Docs, Tasks, and more, mirroring how OpenClaw integrates Google. This is the first **external-CLI-wrapper** capability and is intended to serve as the template for others.

## Why wrap gog instead of building native connectors

- One OAuth flow, one credential set, every Google service — instead of one native connector per service.
- gog owns Google API churn, pagination, scopes, and token storage. Not JIGGA's maintenance burden.
- The user creates their own Google Cloud Desktop OAuth client (bring-your-own — consistent with JIGGA's local-first stance; see `docs/GOOGLE_CALENDAR_RUNTIME_NOTES.md`).

The native Google Calendar connector (from an earlier PR) stays in the registry as a **zero-external-dependency** option for users who only want calendar and don't want to install a Go binary. The two coexist:

- `gog` — broad Google Workspace, requires the gogcli binary.
- `google-calendar` — calendar only, pure Python, no external tool.

## Capability tier

`gog` is an **opt-in first-party capability** (`jigga/optional_capabilities/gog/`). It is NOT bundled — users install it explicitly:

```bash
jigga capabilities install gog
```

The setup wizard verifies gogcli is present, stores the OAuth client, sets a keyring password, runs the interactive OAuth flow, and verifies the connection.

## Subprocess routing — the two halves

gog exercises the `runtime.sandbox` routing rule's nuance precisely:

| gog call | Side | How it runs |
|---|---|---|
| `gog auth credentials <json>`, `gog auth add <email> --services ...` | **Render** (opens browser, needs session env) | `run_gog_interactive` — full user environment + keyring env, inherits TTY, **not** sandboxed |
| `gog gmail search`, `gog calendar events`, ... | **Authority** (acts on Google with the user's creds, run by the possibly-headless supervisor) | `run_sandboxed` — restricted env + keyring backend/password injected via `SandboxSpec.extra_env` |

### Keyring: why the file backend

gog stores tokens in an OS keyring by default, which needs a desktop session bus (`DBUS_SESSION_BUS_ADDRESS` on Linux) — exactly the env the sandbox strips, and which a headless supervisor won't have. So JIGGA configures gog's **encrypted file backend**:

- `GOG_KEYRING_BACKEND=file`
- `GOG_KEYRING_PASSWORD=<password>` — JIGGA stores this at `~/.jigga/secrets/gog_keyring_password` (0600) and injects it into both the interactive and sandboxed gog invocations.

This is what `SandboxSpec.extra_env` was added for: passing an explicit value the parent holds but doesn't export, rather than only allowlisting existing `os.environ` vars.

## Name-collision guard

There is at least one unrelated tool also named `gog` (a node-based script runner; `/usr/bin/gog` on some systems). `gog_binary_status()` does NOT trust the name — it probes `gog auth services` and requires ≥2 distinctive gogcli markers (`gmail`, `calendar`, `drive`, `keyring`, `credential`, `workspace`, `sheets`) in the output. Critically, the marker list **excludes the words we pass as argv** (`auth`, `services`, `doctor`) — a dumb tool echoes our own arguments back, which would trip a naive substring match (this was a real bug caught during development).

## Actions (first slice)

| Action | gog command | Notes |
|---|---|---|
| `gog.gmail_search` | `gog --json gmail search '<query>' [--max N]` | default query `is:unread newer_than:1d` |
| `gog.gmail_get` | `gog --json gmail get <id> [--sanitize-content]` | requires `message_id` |
| `gog.gmail_draft` | `gog --json gmail drafts create --to ... [--subject ...]` | preferred over send |
| `gog.gmail_send` | `gog --json gmail send --to ... [--subject ...]` | **gated**: requires `confirm_send: true` in step input; prefer drafts + `approval: required` |
| `gog.calendar_events` | `gog --json calendar events [--today]` | |
| `gog.calendar_create` | `gog --json calendar create --summary ... [--from/--to/--with-meet]` | requires `summary` |
| `gog.drive_list` | `gog --json drive tree --parent <root\|folder_id> [--depth N]` | defaults to `root` |
| `gog.drive_get` | `gog --json drive get <id> [--fields ...]` | requires `file_id` |
| `gog.drive_share` | `gog --json drive share <id> --to <to> --email <email> [--notify]` | **gated**: requires `confirm_share: true` (external blast radius) |
| `gog.sheets_get` | `gog --json sheets get <ssid> '<range>'` | requires `spreadsheet_id` + `range` |
| `gog.sheets_append` | `gog --json sheets table append <ssid> <table> '<a\|b\|c>'` | takes `row` string or `values` list (joined with `\|`) |
| `gog.docs_get` | `gog --json docs raw <id> --pretty` | requires `doc_id` |
| `gog.docs_write` | `gog --json docs write <id> --append --markdown --text '...'` | appends to user's own doc; requires `doc_id` + `text` |

Output: gog's `--json` stdout is parsed and returned under the `data` key. We pass it through rather than re-normalizing — gog already produces structured JSON, and re-shaping it would mean guessing field names we don't control.

### Gated actions (external/irreversible blast radius)

Two actions are hard-gated — they refuse unless the step input opts in explicitly, on the principle that anything reaching *outside* the user's own account or sending mail needs a deliberate switch:

- `gog.gmail_send` → requires `confirm_send: true`. Prefer `gog.gmail_draft` (always safe) and send from Gmail.
- `gog.drive_share` → requires `confirm_share: true`. Grants someone else access to a file.

Writes to the user's *own* data (`gog.sheets_append`, `gog.docs_write`) are **not** hard-gated — they're additive/reversible and scoped to the user's own documents. Gate them at the workflow level with `approval: required` if you want a human check.

### Tasks deferred

The Google Tasks service is reachable by gog, but its subcommands aren't documented in the gogcli README, so we did not add `gog.tasks_*` actions rather than guess the argv. Add them once the command surface is confirmed.

## CLI

```bash
jigga capabilities install gog       # install + setup wizard
jigga gog status                     # gogcli install + auth state (JSON)
jigga gog login <email> [--services] # run/redo gog auth add
jigga gog logout                     # remove JIGGA's stored keyring password (does NOT revoke Google)
jigga capabilities uninstall gog     # remove manifest + approval + keyring password
```

## Account-ban risk (operational warning)

Per OpenClaw's own guidance, Gmail's automated abuse detection can flag agent-driven access and **ban accounts**. Use a dedicated Google account for agent automation, not your primary. Worth surfacing in user-facing docs.

---

## Template: wrap your own external CLI as a JIGGA capability

`gog` is the reference implementation for "wrap an external command-line tool as a capability." To build your own (e.g. a `gh`, `aws`, or `kubectl` wrapper):

1. **Runtime module** `jigga/runtime/<tool>.py`:
   - `<tool>_binary_status()` — detect the binary AND verify it's the right one (probe a subcommand, match distinctive markers, never match on words you pass as args).
   - A handler `<tool>_handler(step, capability, resolved_input, memory_context, runtime)` that maps `step.action` → argv, runs through `sandbox.run_sandboxed` (authority side), and parses output. Return structured `not_installed` / `not_connected` payloads so workflows degrade gracefully.
   - If the tool needs secrets/config at runtime, inject them via `SandboxSpec.extra_env` (explicit values) or `secrets_required` (allowlist existing env vars).
2. **Optional-capability package** `jigga/optional_capabilities/<tool>/`:
   - `manifest.yaml` — `type: native`, `handler: runtime.<tool>`, the actions, `permissions` (network/secrets), `risk_level`.
   - `__init__.py` exposing `setup(paths, *, input_fn, print_fn, ...)` — the interactive install wizard. Verify the binary, collect any config/credentials, run interactive auth (render-side, NOT sandboxed), verify. Parameterise all I/O so it's testable.
3. **Register**: add an entry to `REGISTRY` in `jigga/optional_capabilities/__init__.py`, and register the handler in `dispatcher.HANDLERS` under `runtime.<tool>`.
4. **CLI (optional)**: add a `jigga <tool> status/login/logout` subcommand for re-auth ergonomics.
5. **Tests**: mock `sandbox.run_sandboxed` and the interactive `runner` — never shell out to the real binary in tests. Cover binary detection (including name-collision rejection), action→argv mapping, output parsing, gating of destructive actions, and the setup wizard with injected I/O.

The whole pattern is designed so a contributor adds a new Google-Workspace-style integration without touching the core runtime — just a new module + optional-capability package + two registrations.

## Follow-up work

- Add Google Tasks actions once gog's Tasks subcommands are confirmed (deferred — undocumented in the gogcli README).
- More Sheets ops (`batch-update` for arbitrary-cell writes — currently only additive `append` is exposed to avoid the overwrite surface) and Docs ops (`format`, `find-replace`).
- Surface `gog`'s `--plain` TSV mode as an alternative output for workflows that want flat text.
- Auto-detect gog's keyring backend choice rather than always forcing `file` (some desktop users may prefer the OS keyring for interactive use).
- Decide whether to deprecate the native `google-calendar` connector once `gog` proves out in real use (currently both ship).

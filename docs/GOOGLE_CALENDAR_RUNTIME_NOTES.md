# Google Calendar Runtime Notes

Third Milestone A slice. The first real third-party connector — reads events from the user's Google Calendar via OAuth 2.0. Also introduces the **optional-capability install** pattern: instead of forcing every JIGGA user through Google Cloud Console setup, Google Calendar is opt-in via `jigga capabilities install google-calendar`.

## What changed

### Optional-capability tier

`jigga/optional_capabilities/` is a new package — a registry of first-party capabilities that ship with JIGGA but are inert until installed. Each entry has:

- `name` (becomes the directory under `~/.jigga/capabilities/<name>/` after install)
- `summary` (shown in the interactive install menu)
- `manifest_path` (the source `manifest.yaml` shipped with JIGGA)
- `setup_fn(paths, *, input_fn, print_fn) -> int` — optional interactive setup that runs after the manifest is copied

This is a parallel mechanism to bundled capabilities (always-on) and user/project-local capabilities (user-provided). Optional-tier packs land in the user-local directory but are sourced from JIGGA's own code.

### New CLI

- `jigga capabilities install` — interactive menu of available optional capabilities.
- `jigga capabilities install <name>` — install directly by name.
- `jigga capabilities uninstall <name>` — remove manifest, drop approval, delete per-capability secrets.
- `jigga capabilities list-available` — print the optional registry.
- `jigga init` now offers the install menu after setup (skipped under `--no-prompt` or non-TTY stdin).
- `jigga calendar status / login / logout` — ergonomic per-service commands for re-auth scenarios *after* install.

### Google Calendar specifics

- New module `jigga/runtime/google_calendar.py` implements OAuth 2.0 + PKCE loopback flow, token storage with auto-refresh, Calendar API client (`events.list`, `events.get`), and the capability handler.
- New runtime path `paths.secrets` (`~/.jigga/secrets/`) created by `init` with `0700` perms. Token + client config files written with `0600`.
- Handler returns a structured `{"status": "google-calendar.not_connected", ...}` payload when no client config or tokens exist — workflows can branch on `status` instead of crashing.
- All HTTP is `urllib.request` (stdlib). No new dependencies.

## OAuth flow details

- **Client type:** Desktop app (Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client IDs → Application type: Desktop app).
- **Redirect:** Loopback (`http://localhost:RANDOM_PORT`). JIGGA picks a free port at flow time; Google's Desktop app type accepts any localhost port.
- **PKCE:** S256 method. We generate the verifier with `secrets.token_urlsafe(64)` and SHA-256 the challenge per RFC 7636.
- **Scopes:** Read-only — `https://www.googleapis.com/auth/calendar.readonly`. Writes are intentionally out of scope for this PR (no `events.insert`/`events.delete`).
- **Token refresh:** Before each API call, `get_valid_tokens()` checks the access token's expiry (with 60s leeway). When expired, it uses the stored refresh token to mint a new access token via `oauth2.googleapis.com/token`. Refresh tokens are usually long-lived but can be revoked by the user in their Google account settings — `jigga calendar login` re-runs the flow to obtain a new one.

## Per the subprocess routing rule

| Subprocess | Routes through | Why |
|---|---|---|
| `webbrowser.open(auth_url)` | Direct call (no sandbox) | Render-side: needs the user's browser env. |
| All API HTTP | `urllib.request` (no subprocess) | Pure Python, in-process. |
| OAuth loopback HTTP server | `socketserver.TCPServer` (no subprocess) | Pure Python, in-process. |

Consistent with the third category locked in by the filesystem capability — native action against a network resource, gated by stored tokens, not subprocess-bound.

## Demo

```bash
# One-time install
jigga capabilities install google-calendar
#   → prompts you for the path to your client_secret.json
#   → opens browser for OAuth consent
#   → verifies with one events.list call
#   → records approval

# Verify
jigga calendar status

# Use it in a workflow
cp examples/demos/google_calendar_briefing.yaml ~/.jigga/workflows/
jigga workflow run google_calendar_briefing

# Later, refresh / disconnect
jigga calendar login   # re-run OAuth (e.g. revoked token)
jigga calendar logout  # delete tokens (keeps client config)
jigga capabilities uninstall google-calendar  # full removal
```

## Design decision: bring-your-own-OAuth-client (locked in 2026-05-29)

For the **local-first runtime** (this repo), JIGGA does not ship a shared OAuth app. Each user creates their own Google Cloud project + OAuth client, and their calendar data flows through credentials they own and can revoke. This is consistent with the local-first philosophy: JIGGA-the-org never holds a shared credential, no centralized trust required, no shared client to secure. The ~3-minute Google Cloud Console setup is acceptable friction for that posture.

A future **cloud version of JIGGA** may use a JIGGA-managed OAuth app to simplify onboarding, but that's a separate product target with a different trust model — out of scope for the local-first runtime. If you're working on this codebase and tempted to land "a shared OAuth app for convenience," that's a design change, not an optimization; surface it explicitly rather than slipping it in.

## Follow-up work

- **Write scope.** `events.insert` / `events.delete` for actually moving the calendar, gated by `permission_mode: ask` so every write requires user approval. Probably its own PR.
- **Multiple calendars / multi-account.** Currently hardcoded to `primary`. Easy to extend `events.list` input to take a `calendar_id`, harder to handle multi-account auth (need per-account token files).
- **Outlook Calendar capability.** Once the optional-install pattern proves out, the same shape extends to Microsoft Graph for Outlook users.
- **Email connector (IMAP read + SMTP draft).** Still open (the last Milestone A gap); Gmail/Workspace users are covered by the `gog` capability meanwhile. Same optional-install pattern.

## Testing notes

- Real OAuth flows are mocked at the `urllib.request.urlopen` boundary — we verify URL construction, token refresh, and event normalization without ever hitting Google.
- The setup wizard is tested with injected `input_fn` and `oauth_runner` so we can drive the install end-to-end (including a "wrong path → correct path" prompt loop) deterministically.
- 31 new tests, including CLI smokes for `capabilities install/uninstall/list-available`, `calendar status/login/logout`, and `init --no-prompt`.

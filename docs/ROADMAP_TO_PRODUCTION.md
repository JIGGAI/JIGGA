# JIGGA Roadmap to Production

Snapshot of where we are, what's specced-but-unbuilt, what's missing for production that the docs don't cover, and a sequenced milestone plan from current state to first production-quality release.

Written 2026-05-29 against branch `refactor/extract-subprocess-sandbox` (122 passing tests, ~4,300 LOC implementation).

---

## Where we are

**Built and stable (the runtime spine):**

| Area | State |
|---|---|
| Supervisor daemon + tick loop + clean SIGTERM shutdown | ✅ |
| Loop prevention (cron dedup + per-agent wake throttle) | ✅ |
| Local file-first state at `~/.jigga/` with atomic writes | ✅ |
| Task queue (create/list/state transitions) | ✅ |
| Agent runner with `permission_mode` (plan_only/ask/accept_edits/autonomous/locked_down) | ✅ |
| Permission evaluators: filesystem, shell, network, scalar resources, memory-scope | ✅ |
| Scoped memory (`includes:`/`excludes:` + retrieval pipeline shape) | ✅ |
| Workflow runner + Terraform-style `plan` / `apply` | ✅ |
| Workflow inference (multi-step session shapes + single-action repetition) | ✅ |
| Model router with dry-run default + OpenAI-compatible provider + fallbacks | ✅ |
| Capability registry with type discriminator (`native`/`skill_pack`/`mcp_server`) | ✅ |
| Capability handlers: 6 bundled dry-run + 2 real (skill_pack via model_router, mcp_server via JSON-RPC stdio) | ✅ |
| First-use approval for user packs + manifest hash drift detection | ✅ |
| Capability security scanner (broad-fs, sensitive paths, suspicious handlers, remote-script install) | ✅ |
| Subagent delegation (`dry_run` + gated `codex_cli` + gated `claude_code`) with sandbox primitive | ✅ |
| Audit log (JSONL, lifecycle events for every major boundary) | ✅ |

**Footprint:** ~4,300 LOC Python + 950 LOC tests, stdlib + PyYAML only. 122 passing tests, ~2.5s suite.

---

## What's left in the planning docs

Each row maps to a doc under `docs/tools/` or `docs/`. The "Has" column is what shipped; the "Missing" column is what the spec wants next.

### Connectors (the gap between demos and real value)

| Domain | Has | Missing |
|---|---|---|
| Email/Calendar | dry-run stubs in `dispatcher.py` handlers | Real Google/iCloud/IMAP/SMTP adapters as capabilities; OAuth flow; drafts-before-send; meeting-prep watcher |
| Notifications | dry-run stub | Real adapter: desktop (`notify-send`/`osascript`/`Windows toast`), urgency routing, quiet hours, digest |
| Filesystem capabilities | `core/io` reads/writes; no capability handlers | `read_file`/`write_file`/`apply_patch`/`search_files` as native capabilities the dispatcher can route to |
| Browser automation | not started | Headless + `isolated`/`user_readonly`/`user_interactive` profiles, domain allowlist, screenshot/extract |
| Safe shell runner | `jigga/tools/safe_process.py` exists in MVP shape | Actually wired into capability dispatch; pty support; background execution sessions |

### Channels (how users actually talk to JIGGA)

| Domain | Has | Missing |
|---|---|---|
| CLI channel | direct CLI calls work | Channel-normalized event flow (so CLI/Slack/email take the same path) |
| Local webhook | not started | HTTP endpoint that supervisor watches; auth |
| Slack / Discord | not started | OAuth + DM/mention routing + outbound reply |
| Email-as-inbox | not started | Watcher pulls actionable email and creates tasks |
| Mobile push | not started | Push action → event payload |

### Observability

| Domain | Has | Missing |
|---|---|---|
| Audit JSONL | ✅ events at every boundary | CLI tail/inspect (`jigga logs tail`, `jigga trace <id>`, `jigga audit --agent X --since 24h`) |
| Secret redaction | none | Middleware that scrubs API keys / tokens / cookies before write |
| Trace IDs | ✅ ambient `trace_id` propagated across tick → agent run → subagent | — |
| Cost tracking | not started | Per-model-call cost; per-agent/per-workflow rollup; budget caps |
| Log rotation | none | Daily rollover, retention policy |

### Memory at scale

| Domain | Has | Missing |
|---|---|---|
| Raw / structured / summary layers | folder shape exists | Real write pipelines beyond the workflow-end raw dump |
| Indexes | folder exists, empty | Keyword index (sqlite FTS5 or whoosh-style); optional vector index behind feature flag |
| Retrieval | scope-based file inclusion | Actual `search_memory(query, scope)` capability |
| Compaction | not started | Summarize completed tasks, archive old raw logs, mark stale facts |
| Memory write proposals | writes happen synchronously | Proposal queue with approval for sensitive types |

### Sessions

| Has | Missing |
|---|---|
| Subagent sessions persisted at `~/.jigga/sessions/<id>/session.json` | The spec's broader Session Manager covering agent / team / workflow / channel / tool runs with a unified API |
| `jigga sessions list/inspect/cancel` | Session summarization, history filters |

### Permissions / Safety

| Has | Missing |
|---|---|
| Per-resource evaluators + permission_mode axis | Approval queue (currently a denied action just stays denied; no human-in-the-loop "approve this one action" path) |
| Capability first-use approval | Project-local capability dir + capability install plan/apply flow |
| Manifest scanner (static) | Runtime monitoring — capabilities that *behave* unexpectedly vs declared |
| sandbox.run_sandboxed seam | Actual OS-level isolation backend (firejail / bwrap / container) |

---

## What production needs that the docs don't yet cover

The planning docs are strong on the "what JIGGA does" axis but quiet on a handful of operational concerns that you'll hit immediately the moment you run JIGGA past your own laptop:

1. **Packaging and install.** Today: `pip install -e .` from the checkout. Missing: pip-publishable package, optional standalone binary, autostart for the supervisor (systemd unit / launchd plist / Windows Service template), uninstall + state-cleanup path.
2. **Secrets.** Today: env-var pull-through via `SandboxSpec.secrets_required`. Missing: a real secrets broker so capabilities request `GITHUB_TOKEN` and get it from a vault/keychain rather than the user's shell env. Mac Keychain / Linux Secret Service / Windows Credential Manager / 1Password CLI integration.
3. **Network isolation per capability.** Today: env scrub on subprocesses. Missing: real egress controls (capability declares allowed domains; subprocess can only reach those — needs DNS/iptables or a proxy). This is the gap between "MCP server can't see my OPENAI_API_KEY" and "MCP server can't exfiltrate data to attacker.com."
4. **Backup, restore, sync.** Today: everything in `~/.jigga/`, no backup story. Missing: encrypted backup target (S3/B2/local), restore command, optional encrypted cloud sync for the subset of state safe to sync (workflows, agents, summaries — not raw transcripts or secrets).
5. **Update model.** Today: `git pull` for jigga itself; capability packs are user-managed files. Missing: `jigga update` for the runtime; `jigga capabilities update <name>` triggering scan + re-approval; rollback.
6. **Telemetry (opt-in).** Today: nothing. For a real product: opt-in error reporting + usage telemetry so you can detect breakage in the wild without violating local-first. Default off; explicit opt-in; documented payload schema.
7. **Cost / budget enforcement.** Today: `max_wakes_per_agent_per_hour` is a rate limit, not a cost limit. Missing: per-agent monthly spend cap, per-workflow run cap, soft warning at 80% / hard stop at 100%.
8. **Crash recovery.** Today: the supervisor loop is single-process. If it crashes mid-tick (after task state transitions but before workflow.run.completed), the task is in a half-state. Missing: idempotent tick semantics + a recovery sweep that detects and resolves stale `claimed`/`running` tasks on startup.
9. **Multi-tenant / multi-machine.** Today: single user, single machine. Missing: if/when a household or team wants shared JIGGA, the state model needs an `owner` field, per-user permission scoping, and a sync story.

---

## Sequenced milestone plan

Each milestone is sized "weeks not months" — assuming the same scope discipline the runtime has shown so far. Roughly: A through E gets you to a "real product you'd let someone else install"; F is the broader v1.0 launch.

### Milestone A — Real connectors (current biggest gap to demo value)
*Goal: the bundled workflows do something users can feel.*

**Status as of 2026-05-29 (after PR #11):**

- ✅ **Notification adapter** (PR #7) — real cross-platform via `notify-send`/`osascript`. Bundled.
- ✅ **Filesystem capabilities** (PR #9) — `read_file`/`write_file`/`list_directory`/`search_files` as a bundled native capability.
- ✅ **Project-local capability discovery** (PR #10) — `<project>/.jigga/capabilities/` with auto-detect from cwd.
- ✅ **Google Calendar via OAuth** (PR #11) — first opt-in first-party capability; also introduced the **three-tier capability model** (bundled / opt-in first-party / user-or-project-local).
- ⏭️ **Email connector** (IMAP read + SMTP draft) — next slice. Ships as opt-in first-party (`jigga capabilities install email`).
- ⏭️ **iCal stopgap** (small follow-up after email) — covers Apple/iCloud users and public calendar feeds. Also opt-in first-party — no OAuth, just an iCal URL.
- ⏭️ **Outlook Calendar** (later) — same opt-in pattern, Microsoft Graph instead of Google.

**Convention locked in by PR #11:** every real third-party connector ships as an opt-in first-party capability under `jigga/optional_capabilities/`. Users who don't want it never see it. No connector goes into `BUILTIN_CAPABILITY_DATA` — bundled is reserved for universal local primitives (filesystem, notifications, subagent-delegation, summarization).

**Convention locked in for first-party connectors:** bring-your-own-OAuth-client / bring-your-own-credentials. JIGGA-the-org never holds a shared credential. A future cloud version of JIGGA is a separate product target with a different trust model — out of scope for this codebase.

**Exit:** the example `morning_day_summary` workflow produces a real summary on a real calendar/inbox and shows up as a real desktop notification. Currently ~80% there (Google Calendar + notifications + filesystem are real; email is the gating item).

### Milestone B — Channels (how invocations reach the runtime)
*Goal: JIGGA responds to events that didn't come from a CLI.*

- Channel-normalized event flow: every external invocation goes through the same `JiggaEvent` shape.
- **Local webhook adapter** — minimal HTTP server the supervisor watches; auth via shared secret.
- **CLI channel** formalized through the normalizer (refactor, not new behavior).
- **One real third-party channel** — Slack DM/mention is the natural pick. OAuth, mention/DM routing, outbound reply via the notification router.
- Approval queue UI through the active channel — so when an action is `needs_approval`, the user sees it on their preferred channel and can approve from there.

**Exit:** "Hey JIGGA, summarize my day" from Slack returns the morning briefing; an approval-required step routes back to Slack with an inline approve button.

### Milestone C — Observability & ops (in parallel with A/B)
*Goal: when something goes wrong in production, you can tell what happened.*

- ✅ **Audit query CLI** — `jigga logs tail`, `jigga audit --agent X --type T --since 24h --status S`, `jigga trace <id>`. (`docs/OBSERVABILITY_RUNTIME_NOTES.md`)
- ✅ **Secret redaction** on every audit write (key-based + value-pattern based).
- ✅ **Trace ID propagation:** an ambient `trace_id` (ContextVar bound at supervisor-tick / run_agent / run_workflow / channel ingest) threads through every event, so `jigga trace <trace_id>` returns the full tree supervisor tick → agent run → tool call → spawned subagent. Run records/artifacts carry it too.
- ⏭️ **Daily log rotation** with retention policy in `config.yaml` (log grows unbounded today).
- ⏭️ **Per-model-call cost recording** (input/output tokens × provider rate) → per-agent/per-workflow rollups.
- ⏭️ **Per-agent budget caps** + soft-warn audit event at 80% / `policy.denied` at 100%.

**Exit:** you can answer "what did `daily_briefing_agent` cost me this week?" with one CLI call. (Audit/trace shipped; cost + budgets remain.)

### Milestone D — Memory at scale
*Goal: long-running agents don't drown.*

- Keyword index (sqlite FTS5) over raw memory; `search_memory(query, scope)` capability.
- Optional vector index behind a feature flag — embed via model router or local model.
- Compaction pipeline: summarize completed tasks weekly, archive raw logs older than N days, mark stale facts.
- Memory write proposal queue for sensitive types (`fact`, `preference`, `relationship`) — writes are batched, the user approves a digest.

**Exit:** memory size is bounded and retrieval gets faster as it grows.

### Milestone E — Real isolation
*Goal: a misbehaving capability can't ruin your day.*

- OS-level sandbox backend behind `runtime.sandbox.run_sandboxed`. Linux: `bwrap` or `firejail`. macOS: `sandbox-exec`. Windows: `WinSandbox` / `JobObject`. Behind a config flag; off by default until per-platform UX is right.
- Secrets broker: macOS Keychain / Linux Secret Service / Windows Credential Manager + a YAML mapping of secret names → broker keys. Capabilities request by name; broker resolves; subprocess gets the value without it ever touching the user's shell env.
- Per-capability network egress allowlist (the missing half of the env-scrub story). DNS-level or proxy-based; chosen per platform.
- Browser automation capability (the highest-blast-radius missing piece) — only built after this milestone because it must run inside the OS sandbox.

**Exit:** even a capability marked `risk_level: high` can be approved knowing the worst case is bounded.

### Milestone F — Distribution & UX
*Goal: someone other than the person who built it can install and run it.*

- Pip-publishable package (`pip install jigga`).
- Supervisor autostart templates (systemd / launchd / Windows Service) + a `jigga install-service` helper.
- Headless-first GUI/dashboard — Electron or web-app pointing at a local API. Read-only at first: state, audit log, sessions, capability registry. Approve actions in v1.1.
- Capability marketplace UX: `jigga capabilities search <query>`, `jigga capabilities install <name>`, `jigga capabilities update`. Backed by a static registry index (git-based, no server needed for v1).
- Encrypted backup with `jigga backup create / restore`. Cloud sync as an optional layer (S3-compatible, age-encrypted).
- Opt-in telemetry: documented payload, off by default, `jigga telemetry on/off` toggle.
- Crash recovery sweep on supervisor startup: any task in `claimed`/`running` for more than the configured runtime gets marked `failed` with an audit event.
- `jigga update` (runtime self-update) + migration scripts for state-shape changes.

**Exit:** v1.0 release-ready. A user with no JIGGA context can `brew install jigga` (or equivalent), run `jigga init`, and have something working in five minutes.

### Milestone G — Post-v1 expansions (don't block launch)

- Multi-user / multi-machine state sync.
- Team permission management.
- Voice channel (Whisper or platform API).
- Real-time streaming for long-running capability invocations.
- Capability marketplace with social proof / reviews / signed publishers.

---

## Decision points before starting

These choices will compound through the milestones; worth deciding before A.

1. **Where do non-trivial connectors live — in-tree or as external capability packs?** Argument for in-tree: discoverability, integrated tests, one install. Argument for external: faster iteration, smaller core, real exercise of the user-pack approval/scan flow. My lean: external, packaged in their own repos but published under the JIGGAI org. The `morning_day_summary` example doc explains how to install the calendar/email packs the first time.
2. **Cross-platform now or Linux-only first?** macOS + Linux for v1 is realistic; Windows can lag a release if the secrets broker / sandbox / autostart take longer there.
3. **GUI or pure CLI for v1.0?** Headless-first is the spec's instinct, but a read-only dashboard significantly broadens who can adopt. My lean: pure CLI for the alpha/private beta; ship a minimal dashboard at v1.0.
4. **Telemetry default.** Even opt-in telemetry is a brand-shaping decision. My lean: ship v1.0 without telemetry; add opt-in in v1.1 once there's a clear question telemetry would answer.
5. **Model-provider posture.** Today: dry_run default, OpenAI-compatible fallback. For v1.0 it's worth deciding whether you ship with first-class Anthropic + OpenAI clients or stay generic. The model router seam already supports both; the question is which the install-default points at.

---

## Recommended next concrete PR

If you want the smallest unit of forward motion that compounds:

**Notification adapter (Milestone A first slice).** ~300 LOC, ~10 tests. Replaces the dry-run `notifications.send` handler with a real cross-platform sender. Zero new architectural decisions. Lets the morning briefing demo actually feel like a personal AI worker.

After that, the natural next two PRs are (i) project-local capability discovery + filesystem capabilities (still Milestone A), and (ii) the audit-log CLI surface (Milestone C, small) — which together unlock the connectors workstream and give you operational visibility for everything that follows.

---

## Design conventions worth keeping

These are rules the runtime has converged on. They aren't enforced by any test or linter today; they're conventions to apply when extending the system. Worth re-reading before a milestone that adds many new spawners, handlers, or capabilities.

### Subprocess routing rule

When adding a new subprocess invocation anywhere in the runtime, decide which side of this line it falls on:

| Side | Examples | Rule |
|---|---|---|
| **Authority** — process acts on external systems with the agent's credentials | `codex_cli` / `claude_code` subagents, MCP servers, future shell runner, future headless browser | **MUST** use `runtime.sandbox.run_sandboxed`. Inherits env allowlist + cwd + timeout; one place to plug in OS-level isolation later. |
| **Render** — process renders output to the user's local UX and needs the user's session env to function | `notify-send`, `osascript display notification`, future tray-icon helpers, future system audio output | **MUST NOT** use `runtime.sandbox.run_sandboxed`. Sandboxing strips `DISPLAY` / `WAYLAND_DISPLAY` / `XDG_RUNTIME_DIR` / `DBUS_SESSION_BUS_ADDRESS` which these tools need, and there's no security gain — they don't carry agent authority. |

Mental check when introducing a new spawner: "does this process act with the agent's authority on external systems, or just render output to the user's desktop?" Authority side: sandbox. Render side: don't.

The rule lives canonically in `jigga/runtime/sandbox.py`'s module docstring; this section mirrors it so reviewers see it during milestone planning, not only at refactor time.

### Capability handler location rule

Per-capability handlers belong in their own module under `jigga/runtime/` (e.g. `notifications.py`, `mcp_client.py`) and are registered into `dispatcher.HANDLERS` from there. The dispatcher itself should stay a routing layer — `_calendar_handler` / `_email_handler` / `_summarization_handler` are vestiges of the MVP shape and will move out as those connectors become real. Milestone A is where this naturally happens.

### Audit-event naming rule

Every action that crosses a trust boundary or external surface emits a JSONL audit event. Naming pattern: `<domain>.<verb>[.<modifier>]`. Existing examples — `subagent.spawn.planned/started/completed/failed`, `capability.invocation.started/completed`, `notification.delivered/failed`, `policy.evaluated`, `policy.denied`, `supervisor.cron_deduplicated`, `supervisor.wake_throttled`. New spawners and handlers should follow this shape so the future `jigga logs tail` / `jigga trace` CLI work uniformly.

---

## Risk register

A few sharp edges worth flagging now so they're not surprises:

- **MCP servers can hang.** Our single-shot batched exchange handles well-behaved servers, but a streaming server or one that waits for stdin past the messages we send will hit the timeout rather than respond cleanly. Either move to a proper async MCP client when the first real one (GitHub MCP, etc.) lands, or document the constraint loudly.
- **`evaluate_capability_permissions` is becoming a hot path.** Every plan + every step goes through it. Currently it walks lists each time; if a workflow has many steps and the capability declares many paths, this gets quadratic. Worth profiling before Milestone D adds search-memory traffic on top.
- **The `permission_mode: ask` semantics rely on per-step `approval: required`.** If a workflow author forgets to mark a write step as `approval: required`, `ask` mode silently lets it through. Worth a planning-time lint that flags steps which look risky (write/send/publish action names) but lack approval.
- **Sandbox `secrets_required` trusts the manifest.** A user-local capability declaring `secrets_required: [OPENAI_API_KEY]` will get it pass-through. First-use approval is the gate; reviewers must understand that approving a pack with `secrets_required` is approving the secret access.

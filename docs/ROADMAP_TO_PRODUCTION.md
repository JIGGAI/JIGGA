# JIGGA Roadmap to Production

Snapshot of where we are, what's specced-but-unbuilt, what's missing for production that the docs don't cover, and a sequenced milestone plan from current state to first production-quality release.

Written 2026-05-29 against branch `refactor/extract-subprocess-sandbox` (122 passing tests, ~4,300 LOC implementation).

> **Status as of 2026-07-29** (963 tests on main; ~21k LOC): Milestones A
> (**complete** — provider-agnostic email shipped as `email-imap`, #155), B, C,
> D, and Teams & Workspaces (including W3 ticket lanes, #136) are **done**.
> Workflow engine v2 (DAG + resumable runs + approval nodes) **merged** (#149).
> The 2026-07-27 capability wave also shipped: **skills as a top-level feature**
> (trigger injection + `jigga skills`, #153), **web.fetch/web.search** with
> pluggable providers (DDG/SearXNG/Brave packs, #154/#159), **shell.run** over
> the safe-process runner (#156), and **one-shot reminders** (#157). Much of
> Milestone F is in: PyPI (#145), service autostart/stop/start/`--system`
> (#73/#143/#144), `jigga update` (#103), `doctor --fix` (#75/#142), jiggaview
> as a plugin. **Open:** Milestone E (isolation, secrets broker, OS-level
> egress) in full; from the production-needs list: backup/restore, telemetry,
> crash-recovery sweep; more channels (Slack/Discord/webhook/iMessage);
> marketplace UX; W7 (#63); media nodes (#150); event triggers (#151).
> Line-items below are kept as written; trust this banner and the ✅ marks where
> they disagree.

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
| Model router: dry-run default + OpenAI-compatible + **ChatGPT-subscription (`chatgpt_oauth`)** providers + fallbacks | ✅ |
| Capability registry with type discriminator (`native`/`skill_pack`/`mcp_server`) | ✅ |
| Capability handlers: 6 bundled dry-run + 2 real (skill_pack via model_router, mcp_server via JSON-RPC stdio) | ✅ |
| First-use approval for user packs + manifest hash drift detection | ✅ |
| Capability security scanner (broad-fs, sensitive paths, suspicious handlers, remote-script install) | ✅ |
| Subagent delegation (`dry_run` + gated `codex_cli` + gated `claude_code`) with sandbox primitive | ✅ |
| Audit log (JSONL, lifecycle events for every major boundary) | ✅ |

**Footprint:** stdlib + PyYAML only; 905 passing tests as of 2026-07-27 (920 once PR #149 merges).

### Shipped since the original plan (off-sequence — recorded here so the plan matches reality)

After Milestone C completed, work pivoted (user-directed) to making a real team *think*, ahead of the documented sequence. These shipped and are now part of the baseline:

- **ChatGPT-subscription model provider** (`chatgpt_oauth`, PR #27) + **JIGGA-native login & onboarding** (PR #28) — run on a ChatGPT Plus/Pro subscription with no API key (Responses API on `chatgpt.com/backend-api`, PKCE login: browser-paste / device-code, own credential store; codex store as fallback). **This resolves Decision Point #5** (see below). `docs/CHATGPT_OAUTH_PROVIDER.md`.
- **Model-backed workflow steps** (`draft_with_model`, PR #29) — a workflow step can route its brief through the agent's model and chain prose by named outputs, so a team becomes a declarative workflow. `docs/MODEL_BACKED_WORKFLOWS.md`.
- **Marketing-team example** (PR #30) — `jigga init --examples` ships a lead→copywriter→SEO `team_launch` workflow.

These are real and tested, but they were **not** milestone items; the milestone sequence below is otherwise unchanged. (Milestone B has since completed — see its section.)

---

## What's left in the planning docs

Each row maps to a doc under `docs/tools/` or `docs/`. The "Has" column is what shipped; the "Missing" column is what the spec wants next.

### Connectors (the gap between demos and real value)

| Domain | Has | Missing |
|---|---|---|
| Email/Calendar | ✅ Google (OAuth + gog) and ✅ provider-agnostic IMAP/SMTP (`email-imap`, #155) with drafts-before-send | iCloud/iCal read; Outlook/Graph; meeting-prep watcher |
| Notifications | dry-run stub | Real adapter: desktop (`notify-send`/`osascript`/`Windows toast`), urgency routing, quiet hours, digest |
| Filesystem capabilities | `core/io` reads/writes; no capability handlers | `read_file`/`write_file`/`apply_patch`/`search_files` as native capabilities the dispatcher can route to |
| Browser automation | not started | Headless + `isolated`/`user_readonly`/`user_interactive` profiles, domain allowlist, screenshot/extract |
| Safe shell runner | ✅ wired into dispatch as `shell.run` (#156): argv-only, high-risk gated, policy floor | pty support; background execution sessions |
| Web read/search | ✅ `web.fetch` + `web.search` (#154), pluggable search providers incl. self-hosted SearXNG + Brave (#159) | browser automation stays behind Milestone E |

### Channels (how users actually talk to JIGGA)

> ✅ **(2026-07 update) The Milestone B rebuild happened** — normalized gateway + `ChannelAdapter` contract (PR #32), supervisor-owned always-poll (PR #34, exponential backoff PR #141), activation modes (PR #35), `jigga channels setup` wizard (PR #36), approval-queue-via-channel (PR #37), and a **webchat** channel for the jiggaview Chat page (PR #123). The table below reflects what's still missing.

| Domain | Has | Missing |
|---|---|---|
| Telegram | ✅ normalized adapter, supervisor-polled, activation modes, onboarding wizard | Webhook (push) mode; richer message types |
| Webchat (jiggaview Chat page) | ✅ file-backed adapter (PR #123) + thread context/transcripts (#128-#132) | — |
| Normalized event flow | ✅ `JiggaEvent` + gateway normalizer + identity/policy gate | — |
| Always-on polling | ✅ supervisor-owned (heartbeat + long-poll, fault backoff) | — |
| Channel onboarding | ✅ `jigga channels setup` (pluggable catalog) | — |
| Activation modes | ✅ `always` / `mention` / `direct_message_only` / `disabled` | — |
| Approval queue via channel | ✅ `approve <code>` / `deny <code>` (PR #37) | — |
| Local webhook | not started | HTTP endpoint the supervisor watches; auth |
| Slack / Discord | not started | OAuth + DM/mention routing + outbound reply (Slack deferred until a Slack app exists) |
| Email-as-inbox / Mobile push / SMS bridge (iMessage) | not started | Per the channel doc; iMessage is macOS-only |

### Observability

| Domain | Has | Missing |
|---|---|---|
| Audit JSONL | ✅ events at every boundary; ✅ CLI tail/inspect (`jigga logs tail`, `jigga trace <id>`, `jigga audit --agent X --since 24h`) | — |
| Secret redaction | ✅ key-pattern + value-pattern scrubbing on every audit write | — |
| Trace IDs | ✅ ambient `trace_id` propagated across tick → agent run → subagent | — |
| Cost tracking | ✅ per-call cost on `model.call`; `jigga cost` rollups; opt-in per-agent budget caps (hard-stop + 80% warn) | Per-workflow rollup; running ledger (with log rotation) |
| Log rotation | ✅ supervisor rolls over by day/size into dated archives + retention prune; `jigga logs rotate` | Running cost ledger (optimization) |

### Memory at scale

| Domain | Has | Missing |
|---|---|---|
| Raw / structured / summary layers | ✅ raw dump + team/role write pipeline (`team.jsonl`/`pinned.jsonl`/role `MEMORY.md`) via `memory.remember` | structured-layer pipelines (preferences/relationships) |
| Indexes | ✅ sqlite FTS5 keyword index (`memory/indexes/`, rebuild-on-stale; scan fallback) | optional vector index behind a feature flag |
| Retrieval | ✅ `memory.search` capability + `jigga memory search` (scope-aware, ranked) | — |
| Compaction | ✅ daily (supervisor) + `jigga memory compact`: archive old raw, stale team facts → `team.archive.jsonl`, finished tasks | model-backed task *summaries* (today archives, doesn't summarize) |
| Memory write proposals | ✅ opt-in `memory.require_approval`: sensitive types park as proposals → `jigga memory proposals`/`approve`/`reject` | — |

### Sessions

| Has | Missing |
|---|---|
| Subagent sessions persisted at `~/.jigga/sessions/<id>/session.json` | The spec's broader Session Manager covering agent / team / workflow / channel / tool runs with a unified API |
| `jigga sessions list/inspect/cancel` | Session summarization, history filters |

### Permissions / Safety

| Has | Missing |
|---|---|
| Per-resource evaluators + permission_mode axis | ✅ Approval queue shipped (B6: code-gated, channel-routed `approve <code>`) |
| Capability first-use approval | ✅ Project-local capability dir (PR #10) + `jigga capabilities install/approve/pending` shipped |
| Manifest scanner (static) | Runtime monitoring — capabilities that *behave* unexpectedly vs declared |
| sandbox.run_sandboxed seam | Actual OS-level isolation backend (firejail / bwrap / container) |

---

## What production needs that the docs don't yet cover

The planning docs are strong on the "what JIGGA does" axis but quiet on a handful of operational concerns that you'll hit immediately the moment you run JIGGA past your own laptop:

1. **Packaging and install.** ✅ Mostly shipped: PyPI package (`pipx install jigga`, PR #145), supervisor autostart (`jigga service install [--system]`, launchd/systemd, PRs #73/#144), `service stop/start/uninstall` (PR #143). Still missing: optional standalone binary, Windows Service template.
2. **Secrets.** Today: env-var pull-through via `SandboxSpec.secrets_required`. Missing: a real secrets broker so capabilities request `GITHUB_TOKEN` and get it from a vault/keychain rather than the user's shell env. Mac Keychain / Linux Secret Service / Windows Credential Manager / 1Password CLI integration.
3. **Network isolation per capability.** Today: env scrub on subprocesses. Missing: real egress controls (capability declares allowed domains; subprocess can only reach those — needs DNS/iptables or a proxy). This is the gap between "MCP server can't see my OPENAI_API_KEY" and "MCP server can't exfiltrate data to attacker.com."
4. **Backup, restore, sync.** Today: everything in `~/.jigga/`, no backup story. Missing: encrypted backup target (S3/B2/local), restore command, optional encrypted cloud sync for the subset of state safe to sync (workflows, agents, summaries — not raw transcripts or secrets).
5. **Update model.** ✅ `jigga update` shipped (PR #103: reconciles recipes, config keys, service). Still missing: `jigga capabilities update <name>` triggering scan + re-approval; rollback.
6. **Telemetry (opt-in).** Today: nothing. For a real product: opt-in error reporting + usage telemetry so you can detect breakage in the wild without violating local-first. Default off; explicit opt-in; documented payload schema.
7. **Cost / budget enforcement.** ✅ Shipped: per-agent budget caps with soft warning at 80% / hard stop at 100% (Milestone C), plus a derived spend ledger (H1a) and model rate-limit resilience (PR #146). Still missing: per-workflow run cap.
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
- ✅ **Google Workspace via `gog`** (`runtime/gog.py`, `optional_capabilities/gog/`) — opt-in first-party capability wrapping the `gog` CLI: **Gmail** (`gmail_search`/`get`/`draft`/`send` — send gated), Calendar, Drive, Sheets, Docs. Keyring-backed; auth via `jigga gog login --services gmail,calendar,…`. **This covers email for Gmail/Google Workspace users** (the roadmap's "email connector" line predated it).
- ✅ **Provider-agnostic email** (#155) — `email-imap` opt-in pack: IMAP search/read, file-first local drafts, SMTP send (approval-gated), setup wizard with app-password guidance. Milestone A is complete.
- ⏭️ **iCal stopgap** — covers Apple/iCloud users and public calendar feeds. Opt-in first-party — no OAuth, just an iCal URL.
- ⏭️ **Outlook Calendar / Microsoft 365** (later) — same opt-in pattern, Microsoft Graph instead of Google.

**Convention locked in by PR #11:** every real third-party connector ships as an opt-in first-party capability under `jigga/optional_capabilities/`. Users who don't want it never see it. No connector goes into `BUILTIN_CAPABILITY_DATA` — bundled is reserved for universal local primitives (filesystem, notifications, subagent-delegation, summarization).

**Convention locked in for first-party connectors:** bring-your-own-OAuth-client / bring-your-own-credentials. JIGGA-the-org never holds a shared credential. A future cloud version of JIGGA is a separate product target with a different trust model — out of scope for this codebase.

**Exit:** the example `morning_day_summary` workflow produces a real summary on a real calendar/inbox and shows up as a real desktop notification. For a **Google/Gmail user** this is effectively reachable today (Gmail + Calendar via `gog`, notifications, filesystem are all real). The remaining gap is **provider-agnostic** email/calendar (IMAP/SMTP + iCal) for non-Google users.

### Milestone B — Channels (how invocations reach the runtime) — **DONE ENOUGH** (B5/Slack deferred)
*Goal: JIGGA responds to events that didn't come from a CLI — through a single normalized gateway, always on.*

Telegram already works ad-hoc (poll + reply + allowlist). This milestone builds the **gateway architecture** from `docs/tools/CHANNEL_GATEWAY_MESSAGE_ADAPTERS.md` that the ad-hoc path skipped, and folds in the user's asks (always-poll, onboarding wizard, more channels). Sequenced slices:

- ✅ **B1 — Normalized gateway + `ChannelAdapter` contract** (PR #32). `JiggaEvent` (actor/conversation/message/target) + gateway normalizer + `ChannelAdapter` (poll/send); Telegram refactored onto it; identity/policy layer (allowlist as an identity rule).
- ✅ **B2 — Supervisor-owned polling ("always poll")** (PR #34). Enabled channels poll on the heartbeat; faults contained.
- ✅ **B3 — Activation modes** (PR #35). `always`/`mention`/`direct_message_only`/`disabled`; group messages tagged `restricted_memory` (scope-enforcement is a follow-up with the Memory/Workspaces work).
- ✅ **B4 — Channel onboarding wizard** (PR #36). `jigga channels setup` — pick a channel → guided auth → activation → enable; pluggable catalog.
- ⏭️ **B5 — Slack adapter** — DEFERRED until a Slack app exists. iMessage = later SMS-bridge adapter (macOS-only); Discord/webhook/email-inbox follow the same contract.
- ✅ **B6 — Approval queue through the channel** (PR #37). `needs_approval` parks a code-gated approval and asks on the channel; `approve <code>`/`deny <code>` resumes the held task; `jigga approvals` CLI.

**Status: done enough.** The channel story is complete end-to-end on Telegram (gateway, always-poll, activation, onboarding, approvals). B5 (Slack) is a drop-in second adapter whenever a Slack app is created.

**Exit:** "Hey JIGGA, summarize my day" from Slack *or* Telegram returns the briefing through the normalized gateway, with the bot polling automatically (supervisor) and an approval-required step routing back to the channel.

### Milestone C — Observability & ops (in parallel with A/B)
*Goal: when something goes wrong in production, you can tell what happened.*

- ✅ **Audit query CLI** — `jigga logs tail`, `jigga audit --agent X --type T --since 24h --status S`, `jigga trace <id>`. (`docs/OBSERVABILITY_RUNTIME_NOTES.md`)
- ✅ **Secret redaction** on every audit write (key-based + value-pattern based).
- ✅ **Trace ID propagation:** an ambient `trace_id` (ContextVar bound at supervisor-tick / run_agent / run_workflow / channel ingest) threads through every event, so `jigga trace <trace_id>` returns the full tree supervisor tick → agent run → tool call → spawned subagent. Run records/artifacts carry it too.
- ✅ **Per-model-call cost recording** (input/output tokens × config rate) on every `model.call` event → per-agent rollups via `jigga cost`.
- ✅ **Per-agent budget caps** — opt-in `budgets` config, hard-stop (`budget.exceeded`, deny) at 100% in `call_model`, soft-warn (`budget.warning`) once at 80%.
- ✅ **Log rotation + retention** — supervisor rolls `events.jsonl` over by day/size into dated archives and prunes past `logs.rotation.retention_days`; readers fold archives back in so queries/budgets span rollovers. `jigga logs rotate` forces it.

**Exit:** ✅ **Milestone C complete.** You can answer "what did `daily_briefing_agent` cost me this week?" with one CLI call (`jigga cost --since 7d`), trace a whole run from one id, and the audit log is bounded. The one remaining item is an optimization, not a feature gap: a running per-agent cost ledger (vs. scanning the log per call) if model-call volume grows.

### Teams & Shared Workspaces — adopt the ClawRecipes model — **IN PROGRESS** (Milestone B done)

*Goal: build/manage teams the way `~/ClawRecipes` does — a per-team shared workspace with file-first coordination. ClawRecipes (the OpenClaw-plugin precursor in the JIGGAI org) and its UI `~/clawkitchen` are the reference; JIGGA already shares the file-first/local-first/agents-teams-workflows DNA, so this is adopting a proven model into the Python runtime, not a pivot. Decision (2026-05-31): finish Milestone B first, then this.*

ClawRecipes-parity map — where each feature lands in the JIGGA milestone process:

| ClawRecipes feature | JIGGA home | Notes |
|---|---|---|
| **Per-team workspace** `~/.jigga/workspaces/<team>/` (`roles/`, `work/`, `notes/`, `shared-context/`) | **W1 (this workstream)** | JIGGA state is global today; this adds per-team dirs |
| **Shared-context curator model** — lead-owned `plan.md`/`priorities.md`, append-only `agent-outputs/` + `feedback/` | **W1** | distinct from memory *scopes*; pure file conventions |
| **Team/role memory** (`shared-context/memory/team.jsonl` + `pinned.jsonl`, per-role `MEMORY.md`) | **Milestone D (Memory at scale)** — NOT a Teams slice | memory is its own milestone; the workspace just provides the on-disk locations, D owns indexing/compaction/retrieval |
| **Ticket lanes** (`backlog→in-progress→testing→done`) + `take`/`handoff`/`complete`/`assign` | **W3** | evolve JIGGA's task queue (states+assignee) toward lanes |
| **Markdown recipes + `scaffold-team`** templating (`{{teamId}}`) | **W4** | JIGGA hand-writes yaml today; add a scaffolder |
| **Workflow DAG** (nodes/edges, `human_approval`/media nodes, `workflow-runs/`) | ✅ **workflow engine v2 (PR #149)** — DAG + resumable runs + approval nodes; media nodes remain (#150) | `docs/WORKFLOW_ENGINE_V2_RUNTIME_NOTES.md` |
| **Channel approval-code flow** (Telegram `approve <code>`) | **Milestone B6** | already planned |
| **ClawKitchen UI** (reads workspace files + shells the CLI, no cache) | **Milestone F dashboard** | the blueprint for JIGGA's dashboard |

Slices:

- ✅ **W1 — per-team workspace + shared-context curator model** (#38): `~/.jigga/workspaces/<team>/` (`notes/plan.md` + `shared-context/priorities.md` lead-curated create-only; append-only `agent-outputs/` + `feedback/`; per-member `roles/`); `runtime/workspaces.py`; curator guard (`CuratorError`); `jigga team init|workspace`.
- ✅ **W1.5 — workspaces created on first use + agent binding** (#39): `run_team`/`run_agent` scaffold idempotently (so the workspace exists however the team/agent was created); team members bind to their team workspace, team-less agents get a per-agent one; the agent loop reads the lead-curated plan/priorities into its prompt and appends results to `agent-outputs/` + `notes/status.md` (the read→act→write loop).
- ✅ **W4 (slice 1) — recipe-driven scaffolding** (#40): Markdown-frontmatter recipes + `jigga recipes scaffold <recipe> --id` / `jigga recipes list` (originally under `jigga team`); bundled `examples/recipes/marketing-team.md`. Follow-ups deferred: `cronJobs` (scheduled role work-loops), `agentTools` policy, `files:`/`templates:`, `kind: agent` recipes.
- ✅ **Member→member handoffs (file-first)** — Hardening H3 (2026-06-01): `routing.handoffs` now *execute*. When a `from` member completes its team task, JIGGA creates the next member's task and records it in `shared-context/handoffs.jsonl` (auditable, no ephemeral bus); a hop guard caps cyclic graphs. `jigga team handoff` / `jigga team decisions`; `runtime/handoffs.py`. This is the member→member routing piece; ticket lanes (below) remain separate/deferred.
- ✅ **W3 — ticket lanes** (PR #136, 2026-06-08): per-team lane vocabulary (opt-in `lanes:` on the team — `true` for defaults or a custom list), `runtime/lanes.py`, `jigga team lanes`, `jigga task list --lane` — a work-management mode over the existing task queue, exactly the "likely shape" described here.
- **Team/role memory is NOT a slice here — it's Milestone D.** The workspace provides the on-disk locations (`shared-context/memory/team.jsonl` + `pinned.jsonl`, per-role `MEMORY.md`); Milestone D owns indexing/compaction/retrieval.
- ✅ **W5 — agent context pack** (#60, 2026-06-01): the agent system prompt is assembled from layered files (OpenClaw/ClawRecipes model) so agents wake grounded instead of per-task amnesiacs — `USER.md` → identity → `SOUL.md` → `AGENTS` (role+roster) → `TEAM.md` → `TOOLS` → `MEMORY.md`+dated daily logs+team facts → plan/priorities → task. Generate-unless-authored; honors `restricted_memory` (group sessions omit private USER/MEMORY); dated breadcrumbs in `roles/<member>/memory/<date>.md`. `runtime/context_pack.py`.
- ✅ **W6 — file-backed mailbox** (#62, PRs #100/#101, 2026-06-04): durable agent→agent / human→agent messages as files (`workspaces/<team>/roles/<member>/inbox/<msg_id>.json`). `mailbox.send` bundled capability (delivers to the RECIPIENT's home workspace) + `jigga mailbox send|list`. Unread mail **wakes the recipient within a tick** (synthetic check-inbox task, wake-throttled); surfaced as a private context-pack layer; marked read only after a successful run; never moved/deleted (auditable correspondence, searchable via memory.search). Audit: `mailbox.sent`/`mailbox.read`/`supervisor.mail_wake`.
- ⏭️ **W7 — self-directed protocol boot** (OpenClaw `AGENTS.md` model): instead of only injecting context, give the agent a startup-protocol `AGENTS.md` ("read SOUL → USER → today's memory → MEMORY if private; don't ask permission") + a per-role `HEARTBEAT.md` work-loop, and let it read its own files via tools. Builds on W5 (files now exist); W5's hybrid already injects the core, so this is the autonomous-read upgrade. Bigger; depends on reliable tool-use.
- Remaining OpenClaw per-role files not yet adopted (fold into W6/W7): `IDENTITY.md` (self), `HEARTBEAT.md` (work-loop, ≈ JIGGA wake/cron), `STATUS.md` (≈ `notes/status.md`), team-level dated `memory/<date>.md`.

Pull remaining items in "when it makes sense" rather than all at once.

**Exit (met):** a JIGGA team has a real shared workspace it coordinates through — lead curates `plan.md`/`priorities.md`, members append outputs (read→act→write), teams are scaffolded from recipes, and ticket lanes (W3, PR #136) organize the board. W7 (self-directed protocol boot, issue #63) is the remaining slice.

### Milestone D — Memory at scale — **COMPLETE**
*Goal: long-running agents don't drown.*

- ✅ **D1 — Keyword index (sqlite FTS5) + `memory.search` capability** (PR pending). Indexes `raw/` + `structured/`/`summaries/` into `memory/indexes/`, rebuilds when stale, scope-aware ranked snippets; falls back to a tokenized scan if FTS5 is absent. `jigga memory search`/`reindex`; the `memory.search` capability (now resolves for agents like `content_strategist`).
- ✅ **D2 — team/role memory write pipelines** (PR pending). `runtime/team_memory.py` writes durable knowledge to a team's `shared-context/memory/team.jsonl` (+ `pinned.jsonl`) and per-role `MEMORY.md`; the `memory.remember` capability lets agents persist facts mid-run; the FTS index covers team/role memory with a `team:`/`role:` layer so `memory.search`/`jigga memory search --team` is team-scoped (no cross-team leakage).
- **D5 — pluggable memory backends** (scoped, not built). Formalize the driver pattern for keyword (existing FTS5), vector (opt-in), and graph (opt-in) backends. Default remains file-only; vector and graph are strictly opt-in. Full spec in [`MEMORY_BACKENDS.md`](./MEMORY_BACKENDS.md). Sub-slices:
  - **D5a** — extract existing FTS5 into `backends/file.py` implementing `KeywordIndex` protocol (no behavior change).
  - **D5b** — verifier primitives (`jigga/runtime/verifiers.py`, ports the accepted ClawRecipes PR #299 pattern, Pythonic). Wired into `memory_proposals.propose()` as pre-approval sanity gate.
  - **D5c** — vector backend (`vector_local`), local `sentence-transformers` embedding, `sqlite-vec` store. Upsert on `raw/` write, delete on compaction. This is the "optional vector index behind a feature flag" the original D scope named.
  - **D5d** — graph backend (`graphiti` driver over embedded Kuzu). Extraction pipeline routes through the agent's configured model router at cheap tier by default; historical backfill uses OpenAI Batch API. Every write passes verifiers (D5b) and honors the existing proposal queue.
  - **D5e** — `search_memory()` becomes a router/fuser across active backends (reciprocal-rank fusion default).
  - **D5f** — MCP server exposing `memory.*` tools for external Codex/Claude sessions.
- ✅ **D3 — compaction** (PR pending). `runtime/compaction.py`: archive `memory/raw/*.json` past `raw_retention_days`, stale `team.jsonl` facts → `team.archive.jsonl` (dropped from search), and finished tasks past `task_retention_days`. Runs on the supervisor heartbeat at most once/`interval_hours` (marker-guarded) + `jigga memory compact [--dry-run]`. (Follow-up: model-backed task *summaries* — today it archives rather than summarizes.)
- ✅ **D4 — write-proposal queue** (PR pending). Opt-in `memory.require_approval`: `memory.remember` of a sensitive type (`fact`/`preference`/`relationship`, configurable) parks a proposal in `shared-context/memory/proposals.jsonl` instead of writing silently; `jigga memory proposals` / `approve <id>` / `reject <id>` reviews them (approve commits to `team.jsonl`). Off by default (direct write).
- **Team/role memory** is owned here (not in the Teams & Workspaces workstream): the per-team workspace just provides the on-disk locations (`shared-context/memory/team.jsonl` + `pinned.jsonl`, per-role `MEMORY.md`); this milestone adds writing, indexing, compaction, and `search_memory` over them.

**Exit:** ✅ **Milestone D complete.** Memory is searchable (D1 FTS5), agents write durable team/role knowledge (D2), it stays bounded via compaction (D3), and sensitive writes can be gated behind approval (D4). Optional follow-ups (don't block): vector index behind a flag; model-backed task *summaries* in compaction (today it archives).

### Milestone E — Real isolation
*Goal: a misbehaving capability can't ruin your day.*

- OS-level sandbox backend behind `runtime.sandbox.run_sandboxed`. Linux: `bwrap` or `firejail`. macOS: `sandbox-exec`. Windows: `WinSandbox` / `JobObject`. Behind a config flag; off by default until per-platform UX is right.
- Secrets broker: macOS Keychain / Linux Secret Service / Windows Credential Manager + a YAML mapping of secret names → broker keys. Capabilities request by name; broker resolves; subprocess gets the value without it ever touching the user's shell env.
- Per-capability network egress allowlist (the missing half of the env-scrub story). DNS-level or proxy-based; chosen per platform.
- Browser automation capability (the highest-blast-radius missing piece) — only built after this milestone because it must run inside the OS sandbox.

**Exit:** even a capability marked `risk_level: high` can be approved knowing the worst case is bounded.

### Milestone F — Distribution & UX
*Goal: someone other than the person who built it can install and run it.*

- ✅ Pip-publishable package (`pipx install jigga`, PR #145; `docs/PUBLISHING.md`).
- ✅ Supervisor autostart (launchd/systemd, `jigga service install [--system]` + `stop`/`start`, PRs #73/#143/#144). Windows Service template still open.
- ✅ (largely) Headless-first GUI/dashboard — **jiggaview** runs as a supervised plugin (CLI-as-API, no cache), with Agents/Teams/Chat/Tasks/Workflows/Runs/Tickets pages. Remaining: ClawKitchen-parity tabs (channels, cron-jobs, goals) and the workflow graph view (jiggaview#13).
- ✅ `jigga doctor` (+ `--fix` auto-remediations, PRs #75/#142) — the "can someone else install it" safety net.
- ✅ `jigga update` (runtime reconcile, PR #103). Migration scripts for state-shape changes still open.
- Capability marketplace UX: `jigga capabilities search <query>`, `jigga capabilities install <name>`, `jigga capabilities update`. Backed by a static registry index (git-based, no server needed for v1).
- Encrypted backup with `jigga backup create / restore`. Cloud sync as an optional layer (S3-compatible, age-encrypted).
- Opt-in telemetry: documented payload, off by default, `jigga telemetry on/off` toggle.
- Crash recovery sweep on supervisor startup: any task in `claimed`/`running` for more than the configured runtime gets marked `failed` with an audit event.

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
5. **Model-provider posture.** ✅ **Resolved (2026-05-31).** Ships with three provider kinds: `dry_run` (default), `openai_compatible` (API key), and **`chatgpt_oauth`** (run on a ChatGPT Plus/Pro subscription, no API key — the install-default for users who already pay for ChatGPT). `jigga model setup` lets a new install choose. A first-class Anthropic-native client remains an easy future add (the router seam supports it). See `docs/CHATGPT_OAUTH_PROVIDER.md`.

---

## Recommended next concrete work (updated 2026-07-29)

Milestones A–D, Teams & Workspaces (minus W7), workflow engine v2, and most of F are done. The biggest remaining lift to v1.0 is **Milestone E (real isolation)** — OS sandbox backend, secrets broker, per-capability egress. Working down the production-needs list above, the buildable-now items in value order: **crash-recovery sweep** (item 8 — small, closes a real half-state hole), **`jigga backup create/restore`** (item 4), then Milestone E proper (items 2+3). Smaller open threads: `jigga agents tools <id>` (effective-toolset inspection), media nodes (#150), event triggers (#151), W7 (#63), marketplace UX, telemetry (item 6), additional channels (Slack when an app exists; webhook).

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

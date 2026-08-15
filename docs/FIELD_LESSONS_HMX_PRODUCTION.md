# Field Lessons from the HMX Production Deployment

**Exported 2026-08-14 from the `hairmx` Mac Studio (100.81.189.7) to the JIGGA controller.**

## What this is

JIGGA is being designed for a workload that already exists and already runs in
production — just on the previous generation of the stack (OpenClaw gateway +
ClawKitchen + ClawRecipes workflow worker + `@jiggai/kitchen-plugin-*` + a BFF
dashboard). That deployment has been live for months across two customers and
has failed in maybe two dozen distinct, instructive ways.

This document is the scar tissue. Every item below is a *real incident with a
date*, not a hypothetical. Each is written as: what broke → why it generalizes →
**what to check in JIGGA**.

The JIGGA repo already has `reference_clawrecipes.md` describing the precursor
*architecture*. This is the complement: the precursor's *operational failure
modes*. The overlap between what broke there and what JIGGA is building is
close to total — JIGGA has workflows with `human_approval` nodes, a supervisor
on a heartbeat, a model router with CLI-backed OAuth providers, channels,
per-team scoped memory, and capabilities. Every one of those has a scar here.

**Provenance:** these are distilled from ~100 memory files on the hairmx box.
Where a claim is machine-specific it is labeled. Nothing here was written from
recollection — each was re-read from its source memory before export.
A file-by-file mapping is in the last section.

---

## Part 1 — What the production system actually is

```
Browser
  └─ Dashboard BFF (:4187, launchd, vanilla JS + node ESM)
       └─ kitchen plugins (@jiggai/kitchen-plugin-{marketing,yot,auth,recruiting,notifications})
            └─ per-team SQLite DBs
OpenClaw gateway (:18789)
  ├─ ClawKitchen (:7777)  — goals/tickets/recipes UI, workflow run viewer
  ├─ ClawRecipes workflow-worker — the actual workflow engine
  └─ llm-task tool — every workflow LLM node routes through here
launchd (~20 plists) — runner ticks, per-agent worker ticks, cron jobs
```

Roughly JIGGA's `supervisor` + `workflow` + `capabilities` + `channels` +
model router, assembled from separate pieces rather than one runtime. **The
seams between those pieces are where nearly every outage lived.** That is the
single most transferable finding: JIGGA's decision to make this one runtime
with one audit trail removes a whole category of these bugs — provided the
internal boundaries don't silently reintroduce the same seams.

---

## Part 2 — The real workload (what a JIGGA tenant looks like)

Two customers on the same codebase, two machines.

### Tenant A — Hair Mechanix (barbershop chain, `hairmx` box)

Three chained workflows in `workspace-hmx-marketing-team/shared-context/workflows/`:

1. **Monthly calendar planning** — cron day 20 @10:00 ET.
   `event_research (analyst) → calendar_strategy (lead) → calendar_draft (lead)
   → human_approval → save calendar .md + .json`
2. **Weekly content generation** — cron Mon @09:00 ET, ~10 nodes.
   `weekly_selection (lead, reads the monthly calendar via {{file:}}) →
   weekly_packet_draft (copywriter, brand skill) → brand_qc (compliance) →
   social_handoff_packet (lead) → save_packet → save_handoff →
   sync_calendar_posts (exec: node script) → save_production_state →
   handoff_social_execution (fire-and-forget to another team)`
3. **Marketing cadence v4** — ad-hoc single-post generation, includes a
   `media-image` node (nano-banana / Gemini) and a QC node.

Then a **social team** with five per-platform publish workflows
(`social-post-to-{instagram,facebook,tiktok,x,google-business}-v1`) that push to
Postiz, which is the actual scheduler.

Agent roles: lead (orchestration/handoff), analyst (research), copywriter
(platform variants), compliance (QC gate), designer (media). Node kinds in use:
`llm`, `tool` (fs.write / exec), `media-image`, `media-video`,
`human_approval`, `handoff`. Run state at
`shared-context/workflow-runs/{runId}/run.json` + `node-outputs/NNN-<node>.json`.

Beyond content there are ~6 more subsystems on the same box: nightly payroll
disbursements, SMS client marketing + recruiting outreach, a field-manager
coverage SMS thread relay, Google review reputation management, and a CRM
(YOT) sync. **These are the "capabilities" a real personal-AI-worker OS gets
asked for within a year of existing.** They are mostly not chat — they are
scheduled jobs that write to a DB, send email/SMS, and need approval gates.

### Tenant B — "Woods" (restaurants, `seven` Mac mini, Intel/Node 22)

Same repo, a stripped trunk (`woods-main`, ~9,500 lines deleted). Then split
again into **two venues** — Oakwood (Dearborn) and Driftwood (Walled Lake) —
each with its own team, social team, calendars, DB, brand file, and **nine
launchd jobs**.

The venues cannot share a pipeline because their brand rules are *mutually
exclusive*: Driftwood is defined by a water view; Oakwood must never imply one.
That is a real multi-tenancy requirement — **per-team config isolation isn't a
nice-to-have, it's a correctness constraint.** The team-scoped brand/ops files
live in `workspace-<team>/shared-context/`, not the workspace root.

**JIGGA implication:** a second customer arrives as a *fork* unless per-team
config is genuinely complete. The Woods fork now costs a cherry-pick per fix
and a "which trunk?" question on every PR. JIGGA's declarative teams are the
right answer — the test is whether a second tenant can be added with **zero**
code deletion.

---

## Part 3 — Lessons by subsystem

### 3.1 Workflow engine

**Declare node output shapes, or the model invents one.** (2026-07-31, woods)
Workflow `llm` nodes invoke `{tool:"llm-task", action:"json"}`. With no declared
output fields the model chooses its own JSON structure, and `{{node.text}}`
substitutes the *raw reply text* — a JSON string. `calendar_draft` returned
`{"markdown_lines":[...]}`, and the save node wrote that JSON object straight
into `current-approved-content-calendar.md`. The file then had **zero
`### Week N` headers**, which the *next week's* workflow parses. The failure
surfaced a week later, in a different workflow, as a content mismatch.

The fix was declaring the field:
`"outputFields":[{"name":"markdown","type":"text"}]` — which makes the runtime
build a JSON schema and enforce it.

**The sharp part:** the identical workflow ran on the other machine for
*months* producing clean markdown, purely because that model happened to reply
with raw text. This class of bug is luck-dependent, not deterministic. It will
pass every test you write and fail on a model upgrade.

> **Check in JIGGA:** every model-backed workflow step must have a declared
> output schema — not optional, not defaulted. A step whose output is consumed
> by a later step's named input should fail *at plan time* if the producing
> step declares no shape. Consider making `jigga workflow plan` reject
> untyped model steps whose output is referenced downstream.

**Step contracts are a two-way obligation.** (2026-06-16, hmx) The `brand_qc`
node sat between draft and handoff. Its output was consumed *two* ways: written
verbatim to the saved packet, AND read by the next node as "the approved
packet". With a review-style prompt and no output contract, the compliance
agent returned a QC *verdict* (`qcDecision`/`hardGateResults`) instead of the
corrected packet. Across five runs only one returned a full packet. Downstream,
the handoff node silently **regenerated all the post content from scratch** —
so the QC gate was in the graph, ran every week, and gated nothing.

> **Check in JIGGA:** a node in the middle of a chain that *transforms* has a
> different contract from one that *judges*. If a node's output is another
> node's primary input, the runtime should know that and validate it. A gate
> whose output isn't consumed as a gate is decorative.

**File includes that never throw hide their own failure.** `{{file:...}}`
substitutes a visible `[[file-include failed: …]]` marker instead of raising
(256KB cap; `..` and absolute paths rejected). Reasonable — but it means a
missing input file can **never** be the explanation for a run that errored, and
we burned real time on woods blaming an empty `calendars/` dir for failures
that were actually a dead OAuth token. Soft-failing includes are fine; they
need to be *loud in the run record* so triage doesn't chase them.

> **Check in JIGGA:** if `{{file:}}`-equivalent substitution soft-fails, record
> the failed include as a first-class event on the run, not just as text in a
> prompt. Then `jigga trace` can say "this run had 3 failed includes."

### 3.2 Approval gates — the highest-stakes subsystem

**Three separate incidents. Take this section seriously; it's where JIGGA's
`human_approval` node lives.**

**(a) An undeliverable approval parks a run forever, silently.** (woods,
2026-06-24 → discovered 2026-07-30) The monthly workflow's `human_approval`
node had `provider: "telegram"`, copied from the machine where Telegram *was*
configured. On woods the Telegram plugin was disabled and no bot token existed
anywhere. The approval request could not be delivered. The run parked at
`waiting_workers`, `nextNodeIndex: 4`, **for 36+ days**. Its `approvals/` dir
was empty and the poller reported "No approval records present" — which reads
identically to "nobody has approved yet."

Because that run never saved the calendar, every *weekly* run for the next
month failed at its second node, and the social handoff (the last node) never
fired — so an entire downstream team had **zero workflow runs, ever**. One
undeliverable message took out a customer's whole content pipeline for over a
month, and nothing alerted.

> **Check in JIGGA:** an approval node must **verify its channel can deliver
> before parking the run**, and fail loudly if not. A parked run with an
> undelivered request must be distinguishable from a parked run awaiting a
> human — different state, and surfaced in `jigga workflow runs`. Add an age
> alarm: any run parked > N hours on approval should notify through a
> *different* channel than the one that failed.

**(b) A second code path to the outside world had no gate at all.** (2026-08-05,
hmx) There were two paths to the publisher: the dashboard (draft → human clicks
Approve → publish) and the per-platform publish workflows, whose node list was
`start → select_account → store_and_publish → end` — **no `human_approval`
node anywhere**. On 2026-08-05 that path scheduled 20 posts nobody approved.
They rendered in the UI as `scheduled`, indistinguishable from human-approved
posts. The customer spotted it, not us.

> **Check in JIGGA:** enumerate every code path that can reach an external
> side effect and prove each passes the same gate. This is an invariant worth
> a test, not a convention. JIGGA's capability/permission registry is the
> natural chokepoint — but only if *nothing* can call a capability's
> underlying implementation directly.

**(c) The gate was fail-open.** Same incident. The publish script *already had*
a `skipPublish` switch. The trigger never set it, so the template rendered as
the literal unsubstituted string `{{trigger.skipPublish}}`, which failed the
truthiness test `in ('1','true','yes','y')` — and it published. Fixed by
inverting the default: anything other than an explicit `false`/`0`/`no` now
means DON'T publish.

> **Check in JIGGA:** unsubstituted templates are the default failure input to
> every boolean guard. Any guard protecting an irreversible action must be
> fail-**closed**, and an unresolved template variable should be a run error,
> not an empty string. If JIGGA renders `{{...}}` anywhere, make unresolved
> references raise by default.

**(d) Un-approval needs a real path.** Once approved, a post is handed to the
external scheduler — flipping local status does nothing, because the *scheduler
is external*. The un-approve had to be a single operation that cascades a
delete to the external system, and a guard sets `newPairs.length = 0` so it
doesn't immediately republish. Used 2026-07-27 to pull back 12 posts already
queued to fire.

> **Check in JIGGA:** approval is not just an inbound decision; there must be a
> *revocation* path that reaches wherever the approved artifact went. Also
> note: approval state was *status-derived* (no dedicated column), which made
> "approved by whom, when" unanswerable — see 3.6.

### 3.3 Supervisor, ticks, and cron

**Short-interval ticks need overlap locks.** (2026-04-28, hmx) 21 launchd
plists firing at `StartInterval: 60`, each spawning a worker tick (~1.1 GB RSS
including plugin load). When the gateway cold-started, tick runtime exceeded
60s and launchd **stacked new ticks on running ones** — 17+ concurrent workers
observed, hammering the gateway during its own model load. The visible symptom
was "OpenClaw takes forever to restart"; the cause was reverse — the worker
storm was overloading the thing it was waiting for.

Fix: wrap each tick in `shlock(1)` on a per-label lockfile.

**But locks alone are not enough at scale.** shlock stops same-label pile-up
and does nothing for **cross-label fan-out**. ~20 labels on the same minute
boundary is still a thundering herd. Needed *all* of:
- per-label lock,
- **random jitter** before the lock (`sleep $((RANDOM % window))`),
- jitter window == the interval (otherwise all fires concentrate in the first
  `window` seconds and the rest of the cycle idles),
- interval sized so `StartInterval ≥ N × avg tick wall time`, targeting
  steady-state concurrency ≤ ~5.

A 120s window proved too tight (concurrency stabilized at 14–17, same as
un-staggered); 300s got it to 4–5. **The trade-off is real and worth stating:
per-agent latency grows with the interval** — at 300s an agent ticks every 5
minutes instead of every 1, and workflow throughput drops proportionally.

> **Check in JIGGA:** the supervisor heartbeat is the analogue. If wake
> throttle + cron dedup are per-agent, verify the *aggregate* behavior with N
> agents due simultaneously. Ask: what is steady-state concurrency at 20
> agents, and what happens when one tick takes longer than the heartbeat?

**Hard-killing a worker orphans its queue claim.** `launchctl kickstart -k`
kills a running job. Doing that to a workflow worker mid-task orphaned its
claim and stalled the run until the **120s lease** expired.

> **Check in JIGGA:** any restart path that can hit a worker holding a lease
> should drain, not kill. And leases need a visible expiry so a stalled run is
> diagnosable rather than mysterious.

**Cold start poisons downstream caches.** (2026.4.26) The CLI's cold start went
to ~25s idle; under fan-out, ticks stretched to 5–6 min. Downstream, ClawKitchen
called `recipes list` with a 120s timeout — the timeout returned **partial
stdout** (just a deprecation banner, no JSON), `exitCode` fell through to `0`,
kitchen treated it as success, and **cached the empty result for 30 minutes**.
Every team/recipe request then returned `Recipe not found: <teamId>`. It could
not self-heal, because all three cache-invalidation routes gated on
`findRecipeById` succeeding first.

> **Check in JIGGA:** never let a timeout produce a cacheable success. Check
> exit status *and* validate the payload shape before caching. And never make
> the recovery path depend on the thing that's broken.

**Boot-time network calls to third parties block startup.** OpenClaw
unconditionally fetched model pricing from `openrouter.ai` (and LiteLLM) on
every gateway boot, regardless of whether the user's config referenced those
providers at all — ~32s stall, sometimes minutes. Workaround was an
`/etc/hosts` blackhole to make it fail in <5ms.

> **Check in JIGGA:** every network call on the startup path should be lazy,
> cached-first, timeout-bounded, and skippable by config. A local-first OS that
> can't boot without the internet isn't local-first.

### 3.4 Model router and CLI-backed providers

**This one is directly on JIGGA's road: JIGGA delegates to locally-installed
`codex` and `claude` CLIs and supports a ChatGPT-subscription OAuth provider.
Here is what that costs in production.**

**OAuth refresh dies independently of the API key, and the error is generic.**
(2026-07-31, woods) Every workflow LLM node failed with:

```
LLM execution failed for node llm:<id>: tool execution failed
ToolsInvokeError  (errorCategory: "unknown")
```

That message contains nothing actionable. The real cause was only in the
gateway's own log file:

```
OAuthRefreshFailureError: OAuth token refresh failed for openai:
OpenAI Codex token refresh failed (401) ... "code":"invalid_refresh_token"
```

Worse: **a working `OPENAI_API_KEY` does not mean the provider works**, because
the provider authenticates through the Codex OAuth path. `curl /v1/models`
returning 200 proves nothing. And the log that had the answer was hidden
because the service plist sent stderr to `/dev/null`.

This cost days. It was initially misdiagnosed as a missing-file problem (see
3.1) and the wrong explanation persisted in notes until corrected.

> **Check in JIGGA:** (1) auth failures must propagate a *typed, specific*
> error to the run record — `errorCategory: "unknown"` on an auth failure is a
> bug in itself. (2) `jigga doctor` should actively probe each configured
> provider end-to-end (a real one-token inference), not just check that
> credentials exist. (3) Never send a daemon's stderr to `/dev/null`.
> (4) Provide a one-line reproducer for "is the model path alive?" that
> doesn't require running a workflow.

**Provider identity changes break configs.** An upgrade merged the standalone
`openai-codex` provider into `openai` with `agentRuntime: {id: "codex"}`, and
every legacy `openai-codex/*` model reference was rejected by the new
validator: `run error: Unknown model: openai-codex/gpt-5.5`. Compounding it, a
strict plugin allowlist *also* began gating bundled plugin discovery, so the
codex runtime silently didn't load ("Codex runtime is selected, but the Codex
plugin is disabled"). The official fix was a `doctor --fix` that rewrote model
refs across defaults, agents, and stale sessions.

> **Check in JIGGA:** config references to providers/models will outlive the
> names. Ship a migration path in `jigga doctor --fix` from day one, and have
> it rewrite **stale session state**, not just config files.

**Local patches to a dependency's dist get wiped by upgrades.** We carried a
three-file patch inside the installed `openclaw` dist for months (an
empty-allowlist guard that broke every tool-disabled LLM call). It survived
plugin reinstalls but was wiped by every OpenClaw upgrade, and had to be
re-applied — once discovered only because a weekly content run failed.

> **Check in JIGGA:** if JIGGA must patch a dependency, make it a runtime
> shim JIGGA owns, and have `jigga doctor` detect and re-apply/alarm.

### 3.5 Capabilities, plugins, and install

**Activation is a three-part handshake, and the failure is silent.**
(2026-06-14) An upgrade changed how the gateway discovers local source-folder
plugins. Kitchen and recipes silently dropped — the gateway booted with only
bundled plugins and the kitchen's port went dark. Three things were *all*
required: (1) source roots listed in `plugins.load.paths`, (2) a manual
`plugins registry --refresh` because the persisted registry goes stale after an
upgrade, and (3) `activation.onStartup: true` declared in the plugin manifest
plus a precompiled `dist/` — the new runtime only *starts* plugins that declare
activation. Older plugin versions predated (3) entirely.

> **Check in JIGGA:** if a capability/plugin fails to load, that must be a
> loud, enumerable state (`jigga doctor` lists "declared but not started"), not
> an absence. An empty capability list looks identical to a working system
> until something calls one.

**A package manager pruned a live symlink and killed all notifications for a
day.** (2026-06-01) The plugin dir loads from `node_modules/@jiggai/*`, with
several plugins wired as `file:` symlinks. Running `npm install` there
**prunes any symlink not listed in `package.json`** as extraneous. A
manually-created `kitchen-plugin-notifications` symlink was never in the
manifest, so an unrelated install silently deleted it — breaking **all SMS and
email notifications** (`ERR_MODULE_NOT_FOUND` on the tick scripts) until
discovered the next day. Fix: every plugin is now a declared `file:` dep.

> **Check in JIGGA:** anything installed out-of-band from the manifest is one
> routine command away from deletion. If JIGGA supports local/dev capabilities,
> they belong in the declared config, and `jigga doctor` should flag any loaded
> capability that isn't declared.

**Two surfaces load the same code; both must reload.** The marketing plugin ran
in *both* the kitchen gateway and in-process inside the dashboard. A rebuild
made the gateway live immediately but the dashboard kept the old module until
its process was restarted. Same class: a module-level cached SMTP transport
captured the password at creation, so replacing a credential file did nothing
until the process was killed.

> **Check in JIGGA:** enumerate every process that loads a capability's code or
> reads a secret, and define what "deployed" means for each. Module-level
> caching of credentials is a footgun — resolve secrets per-use or invalidate
> on change.

**Native modules are per-arch.** `better-sqlite3` built on ARM/Node 25 will not
load on Intel/Node 22. The second customer's box required building **on** the
remote. Relevant to any "copy the dist over" deployment story.

### 3.6 Audit, attribution, and identity

**When 22 posts vanished, we could not say who did it.** (2026-07-23) The
dashboard logged **zero** HTTP requests (its stderr log was ~100MB of a single
repeated port-conflict line). There was no audit table. And `created_by` was
the constant `dashboard-ui` for every row — because the automation posted
through the same API as humans. The only forensic tool available was **diffing
hourly/daily SQLite snapshots** to bound the window. Culprit: permanently
unattributable.

The fix (built 2026-08-06) was a `post_audit` table with a real actor format:
`user:<id>|<email>` for a person, a bare label for automation
(`workflow:weekly-plan-sync`), `system` when unattributed — so
`actor_id LIKE 'workflow:%'` cleanly separates machine from human. That
distinction is *exactly* what was missing when 20 posts auto-published.

Two traps worth copying:
1. **The audit writer throws by design.** A silent gap defeats the purpose — so
   a missing audit table *fails the mutation it was auditing*. Deliberate, and
   correct, but it means audit-schema bugs become write outages.
2. **Migration bookkeeping bit us:** the ORM's journal had stalled, so new
   migrations had to be added to a hardcoded fallback list or a *new* tenant DB
   would never get the table — which, per trap 1, breaks all writes for that
   tenant. Found only by applying it to all six existing tenant DBs.

> **Check in JIGGA:** JIGGA's file-first audit log + trace id is the right
> architecture and is *ahead* of where this deployment was. The gaps to verify:
> (a) does every mutation carry a distinguishable human-vs-agent actor?
> (b) is the audit write on the same failure path as the action (fail-closed),
> and is that survivable? (c) does a *newly created* team get the full audit
> surface, or only teams that existed at migration time?

**Approval had no actor either.** Approval state was derived from status with
no dedicated column — cheap, and it meant "who approved this and when" had no
answer. JIGGA's approval codes should record the approving identity and the
channel it arrived on.

### 3.7 Channels and outbound side effects

**Opt-out state can live outside your database.** The SMS provider records a
**carrier-level opt-out** keyed to the (source number → destination) pair, fully
independent of the local opt-out table. Deleting the local row does **not**
restore delivery — the recipient must text START back to *the same source
number*. Cost RJ a week of silently missing his own daily coverage SMS.

**"Sent" did not mean delivered.** The send path only checked API acceptance
(HTTP 200 + `status: success`). There was **no delivery receipt tracking at
all**, so logs showed `sent` for messages the carrier was blocking.

> **Check in JIGGA:** for any channel, distinguish *accepted* from *delivered*
> in the audit log, and treat provider-side suppression state as authoritative
> over local state. A channel adapter that can only report "we handed it over"
> should say exactly that.

**Route inbound by destination, not by assumption.** All inbound SMS was
processed as client-marketing regardless of which number it arrived at — so
field-manager replies to the operations number landed in the marketing inbox,
and a STOP from an employee would have wrongly opted them out of marketing.
7 of 10 historical inbounds were misrouted. Fixed by branching on the
destination number.

> **Check in JIGGA:** an inbound channel event needs its destination identity
> as a first-class routing key. One bot / one number serving two purposes is
> the norm, not the exception.

**Delete-then-recreate republish orphans work.** Editing a publish-relevant
field on an already-scheduled item ran a cascade: delete the external post →
delete local mapping → resolve media → republish. It was **delete-first,
non-atomic, and failure-swallowing**: if the republish failed (e.g. the new
image exceeded the provider's 10MB cap), the item was left orphaned — gone
externally, no mapping row, still `scheduled` locally — and the API **still
returned 200**. Reconcile couldn't recover it because there was nothing left
externally to reconcile against.

> **Check in JIGGA:** recreate-before-destroy, or make the failure loud. Any
> operation that mutates external state through a local proxy needs to surface
> partial failure rather than return success. Also: validate provider limits
> (size, format) at *attach* time, not at publish time.

**Reconciliation lag looks like a bug.** A daily 05:00 reconcile flipped local
status to match the external scheduler — so an item could publish at noon and
still read "scheduled" locally for 17 hours. Normal, and endlessly confusing.
Document expected lag next to any eventually-consistent state.

### 3.8 Credentials

**One file, four subsystems, silent failures.** A single Gmail app-password
file was read by four separate plugins (dashboard invites, nightly payroll
emails, recruiting outreach, review polling). One bad value broke all four at
once. Google revokes every app password when the account password changes — so
"worked yesterday, 535 today" with no deploy.

**The alert path shared the failing credential.** The nightly payroll job's
fallback alert email sent over the *same* credential — so when the credential
died, the alarm died with it. On 2026-07-30 both payroll emails failed and
nothing surfaced it; it was found days later while tracing an unrelated error.

> **Check in JIGGA:** this is the single best argument for JIGGA's explicit
> policy/permission model — but the lesson is sharper than "scope credentials."
> **An alarm must not depend on the subsystem it monitors.** Notification
> failures specifically need an independent path.

**In-process consumers don't inherit the cron environment.** Cron-launched
scripts got a keyring password from the launchd job env for free. The same code
imported in-process by a long-running server did not, and failed with "no TTY
available for keyring file backend password prompt." Every in-process caller
had to explicitly load the secret from its file and inject it into the child
env.

> **Check in JIGGA:** secret resolution belongs in the runtime, resolved
> identically whether the caller is a scheduled agent, a chat turn, or a CLI
> invocation. Divergent env inheritance across execution contexts is a bug
> factory.

### 3.9 Observability and diagnosis

Diagnostic techniques that repeatedly worked, worth building in rather than
rediscovering:

- **The real error is in the daemon's own log**, not the supervisor's. Plists
  that discard stderr turn every failure into a mystery. `jigga trace` is the
  right idea; make sure it captures provider/tool-level errors verbatim.
- **Reproduce as the actual user.** For a gated web surface, borrowing a live
  session id from the auth DB and curling with it proved a role problem vs. a
  routing problem in seconds. A bare unauthenticated curl proves nothing.
- **Per-node run outputs are the audit trail.** `workflow-runs/<ts>/run.json`
  plus `node-outputs/002-<node>.json` is what let us prove which workflow
  published the 20 unapproved posts, and what the inline script was.
- **Duplicate service definitions hide.** The same launchd label existed in both
  the user and system domains; the user job won the port race and the system one
  respawned every 5s forever, writing 124MB of error log. `launchctl list` only
  shows the user domain, so the second job was **invisible** to the obvious
  check. Worse, the two had different env vars — so which one won the race
  decided whether magic-link URLs and the inbound webhook worked.

> **Check in JIGGA:** `jigga doctor` should detect more than one supervisor/unit
> definition for the same service, across every scope it can be installed in.

### 3.10 Two process lessons that generalize

**Don't ship a producer whose consumer ignores it.** (2026-06-14) A base-photo
rotation subsystem selected photos, wrote a ledger, saved a file, and instructed
the drafting model — but the actual image generator never read *any* of it. It
ran its own picker. The whole subsystem was decorative and the bug it existed to
fix was untouched. RJ's words: *"you built something yesterday that had no
purpose within the context you wrote it."* The smell was that the consumer
already had a parallel mechanism — the fix is to reconcile to one, not run two.

**State only what you verified.** Two of the incidents above (the woods
pipeline, the OAuth failure) had a *wrong* explanation recorded confidently and
propagated for weeks before correction. When exporting knowledge between
systems, provenance matters more than fluency.

---

## Part 4 — Condensed checklist for JIGGA

| # | Assertion to verify in JIGGA | From |
|---|---|---|
| 1 | Model-backed steps whose output is consumed downstream must declare a schema; reject at plan time otherwise | 3.1 |
| 2 | Failed template/file substitutions are recorded as run events, never silently substituted | 3.1 |
| 3 | An approval node verifies channel deliverability *before* parking the run | 3.2a |
| 4 | Runs parked on approval past N hours alarm through a *different* channel | 3.2a |
| 5 | Every path to an external side effect passes the same gate — test it, don't assume it | 3.2b |
| 6 | Guards on irreversible actions are fail-closed; unresolved template vars raise | 3.2c |
| 7 | Approval has a revocation path that reaches the external system | 3.2d |
| 8 | Supervisor behavior is characterized at N agents due simultaneously, not just one | 3.3 |
| 9 | Restarts drain leases rather than orphan them | 3.3 |
| 10 | A timeout can never produce a cacheable success | 3.3 |
| 11 | No blocking third-party network call on the startup path | 3.3 |
| 12 | Auth failures surface typed, specific errors — not `unknown` | 3.4 |
| 13 | `jigga doctor` probes each provider with a real inference, not a credential check | 3.4 |
| 14 | Config migration for renamed providers/models rewrites stale session state too | 3.4 |
| 15 | A capability that fails to load is a loud enumerable state, not an absence | 3.5 |
| 16 | Nothing loaded out-of-band from the declared config | 3.5 |
| 17 | "Deployed" is defined per-process for every surface that loads the code | 3.5 |
| 18 | Every mutation records a human-vs-agent-distinguishable actor | 3.6 |
| 19 | A newly created team gets the full audit/migration surface | 3.6 |
| 20 | Channels distinguish accepted from delivered; provider suppression state wins | 3.7 |
| 21 | Inbound events route by destination identity | 3.7 |
| 22 | External mutations recreate-before-destroy, or fail loudly | 3.7 |
| 23 | Alarm paths never depend on the subsystem they monitor | 3.8 |
| 24 | Secrets resolve identically across cron / chat / CLI execution contexts | 3.8 |
| 25 | `jigga doctor` detects duplicate service definitions across all install scopes | 3.9 |
| 26 | A second tenant is addable with zero code deletion | Part 2 |

---

## Part 5 — Provenance

Each section traces to a memory file on the hairmx box
(`~/.claude/projects/-Users-hairmx/memory/`). Re-read the source before acting
on anything load-bearing; these were current as of 2026-08-14.

| Section | Source memory |
|---|---|
| 3.1 output shapes | `reference_workflow_llm_output_fields.md` |
| 3.1 step contracts | `reference_brand_qc_packet_contract.md` |
| 3.1 file includes | `reference_llm_task_oauth_failure.md` |
| 3.2a undeliverable approval | `project_woods_pipeline_stalled_telegram_approval.md` |
| 3.2b/c ungated path, fail-open | `project_social_workflow_auto_publish.md` |
| 3.2d revocation | `reference_unapprove_post_back_to_draft.md`, `project_per_post_approval.md` |
| 3.3 tick overlap, jitter | `feedback_shlock_launchd_plists.md` |
| 3.3 cold start, cache poisoning | `project_openclaw_cli_startup_regression.md` |
| 3.3 boot-time fetches | `project_openclaw_pricing_hosts_block.md` |
| 3.4 OAuth failure | `reference_llm_task_oauth_failure.md` |
| 3.4 provider migration | `project_openclaw_codex_provider_migration.md` |
| 3.4 dist patching | `project_openclaw_llm_task_local_patch.md` |
| 3.5 activation handshake | `project_openclaw_gateway_local_plugin_loading.md` |
| 3.5 symlink pruning, two surfaces | `reference_kitchen_plugin_deploy_mechanism.md` |
| 3.5 native modules | `project_woods_client_machine.md` |
| 3.6 no attribution | `reference_marketing_posts_no_audit_trail.md` |
| 3.6 audit built | `project_marketing_post_audit_trail.md` |
| 3.7 SMS provider | `reference_multitel_sms_provider.md` |
| 3.7 inbound routing | `project_coverage_threads.md` |
| 3.7 cascade, media cap, reconcile lag | `reference_postiz_publish_pipeline_gotchas.md` |
| 3.8 credential SPOF | `reference_gmail_app_password_single_point_of_failure.md` |
| 3.8 in-process env | `feedback_gog_keyring_in_process.md` |
| 3.9 reproduce as user, duplicate jobs | `reference_dashboard_reproduce_as_user.md` |
| 3.10 decorative subsystem | `feedback_verify_end_to_end_integration.md` |
| Part 2 workload | `project_workflow_system.md`, `project_dashboard_architecture.md` |
| Part 2 tenants | `project_woods_client_machine.md`, `reference_woods_venues_oakwood_driftwood.md` |

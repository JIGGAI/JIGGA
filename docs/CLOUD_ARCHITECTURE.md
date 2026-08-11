# JIGGA Cloud Architecture

> **Status:** Design doc. No code yet. Records the open-core strategy, the
> multi-tenancy contract JIGGA (OSS) must expose, and the plane architecture
> for the proprietary hosted product (working name: `jigga-cloud`).
>
> **North-star rule:** every future PR is either OSS (lands in this repo) or
> cloud-only (lands in the closed `jigga-cloud` repo). This doc is where that
> boundary is written down.

## Positioning

**JIGGA is open source.** Anyone can install and run it — laptop, homelab,
their own AWS account. That is a design commitment, not a marketing line.

**JIGGA Cloud is a hosted product.** It runs JIGGA as its per-tenant runtime
unit and adds the multi-tenant control plane needed to sell it as a service.
It is proprietary.

This is the **open-core pattern** (same shape as Zep/Graphiti, Supabase,
GitLab). Developers get a full-featured OSS runtime; operators pay for the
managed platform around it.

## Open-core boundary

| Concern | OSS (this repo) | Cloud (proprietary `jigga-cloud`) |
|---|---|---|
| Runtime, supervisor, agent execution | ✅ | uses OSS |
| Agents / teams / workflows / recipes | ✅ | uses OSS |
| Memory (raw/structured/summaries + FTS5) | ✅ | uses OSS |
| Pluggable memory backends (D5) | ✅ | uses OSS |
| CLI (`jigga …`) | ✅ | ops use OSS CLI |
| Capabilities + permissions + approvals | ✅ | uses OSS |
| Sandboxing (Milestone E) | ✅ | uses OSS + Fly Machines (microVM) |
| Multi-tenancy in the state model (`tenant_id`) | ✅ ships upstream | required by cloud |
| REST API surface for tenant admin + agent invoke + memory query | ✅ ships upstream | required by cloud |
| Fly.io deployment recipe (`Dockerfile`, `fly.toml`, provisioning script) | ✅ ships upstream as an example deployment | cloud reuses |
| Minimal model gateway (routing across providers) | ✅ ships upstream | cloud extends |
| Tenant provisioning orchestrator (Fly Machines API driver) | ❌ | ✅ cloud only |
| Billing + usage metering aggregator (Stripe metered billing) | ❌ | ✅ cloud only |
| Hosted-credit ledger, per-tenant budget enforcement pre-call | ❌ | ✅ cloud only |
| Web UI (signup, dashboard, agent catalog, billing) | ❌ | ✅ cloud only |
| Admin plane (operator UI, cross-tenant health, incident response) | ❌ | ✅ cloud only |
| Backup automation / DR orchestration | ❌ | ✅ cloud only |
| Observability wiring (Axiom / Grafana Cloud) | export hooks only | ✅ cloud only |

**Rule of thumb:** if a feature is valuable to someone running JIGGA on their
own hardware, it belongs upstream. If a feature only exists to make many
tenants pay one operator, it belongs in `jigga-cloud`.

## Reference architecture — three planes

```
                    ┌─────────────────────────────────────────────┐
                    │         CONTROL PLANE  (cloud only)         │
                    │   small, stateful, highly available          │
                    │                                              │
                    │  • API Gateway (auth, rate-limit)            │
                    │  • Tenant Service (CRUD, provisioning)       │
                    │  • Orchestrator (Fly Machines API driver)    │
                    │  • Billing + Metering aggregator (Stripe)    │
                    │  • Web UI backend (Next.js API routes)       │
                    │  • Postgres (tenants, users, plans, keys)    │
                    └───────────────┬─────────────────────────────┘
                                    │ spawn / attach / drain
                                    ▼
                    ┌─────────────────────────────────────────────┐
                    │         DATA PLANE  (JIGGA runtimes)         │
                    │       per-tenant Fly Machines, JIGGA OSS     │
                    │                                              │
                    │  ┌─────────────┐ ┌─────────────┐             │
                    │  │  Tenant A   │ │  Tenant B   │  ...        │
                    │  │  Fly Machine│ │  Fly Machine│             │
                    │  │  = JIGGA    │ │  = JIGGA    │             │
                    │  │  supervisor │ │  supervisor │             │
                    │  └──────┬──────┘ └──────┬──────┘             │
                    │         │               │                    │
                    │  scale-to-zero when idle; ~2-5s warm start   │
                    └─────────┼───────────────┼────────────────────┘
                              │               │
                              ▼               ▼
                    ┌─────────────────────────────────────────────┐
                    │                STATE PLANE                  │
                    │                                              │
                    │  • Per-tenant Fly Volume mounted at          │
                    │    ~/.jigga/  (canonical state)              │
                    │  • Weekly snapshot to R2/S3 (MVP backup)     │
                    │  • Shared Postgres (control-plane metadata)  │
                    │  • Optional shared vector store (later)      │
                    │  • Optional shared graph store (later)       │
                    │  • Model Gateway (OSS core + cloud metering) │
                    └─────────────────────────────────────────────┘
```

**Substrate:** [Fly.io Machines](https://fly.io/docs/machines/). Firecracker
microVMs give strong tenant isolation without operating Kubernetes. Fly Volumes
give per-tenant persistent storage. Fly's private networking + edge routing
handles per-tenant hostnames. Scale-to-zero is built in.

**Trade-off accepted:** we take a hard dependency on Fly.io for MVP. Cloud
migration later is not free, but the OSS runtime remains substrate-agnostic
— any customer or future ops team can move JIGGA to bare Kubernetes, ECS, or
Nomad without changing the runtime code.

## Multi-tenancy contract JIGGA OSS must expose

This is the **upstream PR scope** the cloud depends on. Both JIGGA users
(power users running multi-user installs) and JIGGA Cloud benefit.

### Data model changes

- Add optional `tenant_id: str | None` field to every persisted state object:
  agents, teams, workflows, tasks, memory episodes, audit events, sessions.
- Introduce a `Tenant` record type (id, created_at, name, metadata).
- Filesystem layout gains a tenant dimension **only** when multi-tenant mode
  is enabled: `~/.jigga/tenants/<tenant_id>/` mirrors today's `~/.jigga/`.
- **Single-tenant default is preserved.** Existing installs behave exactly as
  today. Multi-tenant mode is opt-in via config:
  ```yaml
  runtime:
    multi_tenant: true
  ```
- Runtime helpers (`workspace_dir`, `memory_dir`, `tasks_dir`, etc.) accept
  a tenant scope; single-tenant callers pass `None` and get today's paths.

### Runtime changes

- Supervisor tick loops iterate tenants (when multi-tenant); each tick
  processes one tenant's tasks in one supervisor cycle. Fair-share scheduling
  becomes a real concern — MVP uses round-robin.
- Audit events include `tenant_id`. Logs / traces / cost records are already
  keyed on `trace_id`; adding `tenant_id` alongside is mechanical.
- Capability approvals become tenant-scoped. A capability approved for tenant
  A does not carry to tenant B.
- Memory scopes remain per-tenant. `MemoryScope.includes/excludes` paths
  resolve against the tenant's memory tree, not a global one.

### REST API surface (new, upstream)

The CLI-first history means JIGGA lacks a hosted API surface. Cloud requires
one; power OSS users have been asking for it too. Ships in this repo.

- Auth: bearer tokens per tenant (issued by CLI or, in cloud, by the control
  plane).
- Endpoints (v1):
  - `POST /v1/tenants` — create a tenant (multi-tenant mode only)
  - `GET  /v1/tenants/{id}` — inspect
  - `POST /v1/agents` — create/register an agent for a tenant
  - `POST /v1/agents/{id}/invoke` — one-shot invocation with input
  - `POST /v1/tasks` — enqueue a task
  - `GET  /v1/tasks/{id}` — inspect / stream events
  - `POST /v1/memory/search` — query memory (honors scopes)
  - `POST /v1/memory/remember` — write memory (honors proposal queue)
  - `GET  /v1/audit` — audit log (filtered)
- All endpoints are tenant-scoped by the auth token; no cross-tenant reads
  possible via the API.

### Fly.io deployment recipe (new, upstream)

Ships as `deploy/fly/` in this repo:

- `Dockerfile` — pins Python, installs JIGGA via pip, entrypoint runs
  `jigga service run` in foreground.
- `fly.toml` — one Machine per instance, private-network only by default,
  Fly Volume mount at `/data` (mapped to `~/.jigga/`).
- `deploy/fly/README.md` — self-hosting guide.
- `deploy/fly/provision.sh` — reference provisioning script (create app,
  create volume, launch machine, seed tenant).

**Why upstream, not cloud-only?** Because it's an example deployment. Any
JIGGA user who wants to host their own install on Fly benefits. Cloud will
have its own private provisioning script that calls Fly Machines API
directly (no shell scripts), but the reference recipe is public.

### Minimal model gateway (new, upstream)

- Wraps the existing model-router abstraction.
- Routes calls across providers (OpenAI, Anthropic, OpenRouter, local).
- Applies per-agent + per-tenant budget caps **before** the call, not after.
- Emits usage events (`model.call`) with `tenant_id`, `agent_id`, provider,
  tokens, cost — the shape the cloud metering aggregator consumes.
- **Not included upstream:** hosted-credit ledger, Stripe integration,
  BYO-key-vs-hosted-credit routing logic. Those live in `jigga-cloud`.

**Explicit design bet:** open-sourcing the gateway core is worth more than
holding it back. OSS users get real BYO-key routing; the commercial
differentiator is the hosted-credits layer plus metering pipeline, not the
routing itself.

## Cloud plane responsibilities (proprietary)

Documented here for design coherence. Implementation lives in `jigga-cloud`.

### Tenant Service
- CRUD for tenants (create, suspend, delete, purge).
- Owns the mapping from Stripe customer → JIGGA tenant.
- Issues API tokens per tenant.
- Provisioning is asynchronous: create tenant → enqueue provisioning job →
  orchestrator picks it up → Fly Machine spawned → volume mounted → JIGGA
  seeded → status = ready.

### Orchestrator
- Thin wrapper over the Fly Machines API.
- Manages: create machine, attach volume, start/stop, health check, destroy.
- Handles scale-to-zero: idle tenants have their Machine suspended; incoming
  API calls warm-start (~2-5s).
- Owns tenant → Fly Machine ID mapping (Postgres).

### Billing + Metering
- Consumes usage events emitted by the OSS model gateway
  (`model.call` events with `tenant_id`, tokens, cost).
- Aggregates per-tenant per-billing-period.
- Pushes to Stripe metered billing.
- Enforces hard-stop when tenant exceeds their plan.

### Model Gateway (cloud extensions)
- Hosted-credit ledger (a tenant's credit balance in cents).
- BYO-key path: routes calls using the tenant's provided key, meters usage
  for reporting but doesn't debit credits.
- Hosted-credit path: uses our provider account, debits credits per call.
- Pre-call budget check: reject calls that would exceed the tenant's
  remaining credit balance.

### Web UI
- Next.js app. Signup → onboarding wizard → agent catalog → dashboard →
  billing.
- Backend routes are thin — they call the OSS REST API on the tenant's
  Fly Machine plus the cloud control-plane services.

### Product surface v1: catalog + clone-and-tweak (both paths)

Decision: MVP supports **both** curated pre-built teams and user-customized
teams, without shipping a full drag-and-drop builder. Two paths through
the same underlying primitive (JIGGA declarative recipes):

**Fast path — catalog deploy:**
1. Browse catalog of 5-8 curated agent teams (SDR, support triage, ops
   assistant, research analyst, content ops, sales enablement, ...).
2. Click *Deploy* → team seeded into tenant → onboarding wizard collects
   any required integrations/keys → running.

**Power path — clone-and-tweak:**
1. From any catalog entry, click *Clone*.
2. Get an editable copy in the tenant's workspace (recipes, agent MDs,
   team config YAML).
3. Edit recipe files in-dashboard via Monaco / CodeMirror.
4. Save → validate (JIGGA recipe validator) → apply (hot-reload the
   team on the tenant's Fly Machine).

**Explicitly deferred to v2:**
- Full drag-and-drop workflow / capability builder canvas.
- In-UI role permission editor UX (users edit YAML directly for v1).
- Template marketplace (community-shared clones).
- Live agent debugging surface (tail traces, replay tasks).

**Rationale:** JIGGA recipes are already declarative files. A file editor
over a validated schema is a legitimate power-user surface and ships in
~1.5 weeks, versus ~4 weeks for a first-class visual builder. It preserves
the "declarative agents-as-code" ethos of the OSS runtime and lets pilots
customize anything without waiting for v2 UI work. What people actually
customize in the first month becomes the requirements doc for the visual
builder.

**OSS vs cloud split for v1 product surface:**
- Catalog schema, recipe validator, recipe hot-reload semantics: **OSS**
  (upstream to JIGGA — useful for anyone running their own install).
- Catalog *entries themselves* (curated agent teams): **OSS**
  (`recipes/catalog/` in this repo), so self-hosters get the same starter
  packs.
- Catalog browser UI, clone-a-template flow, file editor UX, tenant-scoped
  recipe application: **cloud-only**.

### Admin plane
- MVP: SQL queries against Postgres + Grafana Cloud dashboard on top of the
  event stream. No operator UI in the 8-week plan.
- Post-MVP: proper operator UI with tenant health, cost anomalies, incident
  response actions.

### Observability
- Traces exported from JIGGA runtimes to Axiom (or equivalent).
- OSS-side change: `trace_id` already exists; add `tenant_id` to every
  exported trace event (upstream PR).
- Cloud-side: dashboards, alerting on SLOs (p95 first-response, task-success,
  error budgets per tenant).

## Compressed 9.5-week plan (2 months + ~1.5 weeks for both product paths)

Original target was 8 weeks. Supporting **both** catalog deploy and clone-and-tweak
(see Product surface v1 above) adds ~1.5 weeks for the catalog schema, file
editor UI, and hot-reload path. Kept in scope because the clone-and-tweak
surface is the primary differentiator vs turnkey-only competitors.

| Week | OSS deliverable (this repo) | Cloud deliverable (jigga-cloud) |
|---|---|---|
| 1 | `tenant_id` in state model spike; multi-tenant config flag; two supervisors, one machine, isolation proven | Repo bootstrap; Postgres schema; auth provider wired |
| 2 | REST API scaffold (`/tenants`, `/agents`, `/invoke`, `/memory`) | Tenant service scaffold; talks to OSS REST API |
| 3 | `deploy/fly/` recipe (Dockerfile, fly.toml, provision.sh) | Fly Machines orchestrator; automated tenant provisioning end-to-end |
| 4 | Minimal model gateway (routing + budget + usage events) | Cloud-side model gateway extensions (BYO key path, hosted-credit ledger) |
| 5 | Catalog schema; `recipes/catalog/` seeded with 5-8 curated teams; recipe validator + hot-reload | Next.js UI: signup → onboarding → dashboard → catalog browser |
| 6 | Audit event enrichment (`tenant_id` everywhere) | Clone-a-template flow: catalog entry → tenant workspace copy |
| 7 | Traces export gains `tenant_id`; hooks doc | File editor UI (Monaco/CodeMirror over recipe files) with save → validate → apply |
| 8 | (buffer for OSS follow-ups discovered in weeks 1-7) | Stripe metered billing wired; free tier + paid tier |
| 9 | | Observability wiring (Axiom); alerting on error budgets |
| 9.5 | | Private beta (3-5 pilots); hotfix + launch prep |

**Explicit non-goals for MVP** (deferred, not forgotten):
- HIPAA controls (kept design-clean so it's a later add-on, not a rewrite).
- Multi-region deployment.
- Advanced sandbox hardening beyond what Fly Machines give for free.
- Full admin UI.
- Formal DR (weekly volume snapshot to R2 is MVP-sufficient).
- On-prem / private-cloud SKU.
- Full drag-and-drop visual builder for agents/teams (v2; v1 is file
  editor over declarative recipes).

## PR intake rules

Any future PR against this repo (or `jigga-cloud`) that touches the cloud
boundary must:

1. State explicitly whether it is **OSS** (this repo) or **cloud-only**
   (`jigga-cloud`).
2. If OSS, justify why it's valuable to someone running JIGGA on their own
   hardware.
3. If cloud-only, confirm it does not require changes to OSS interfaces that
   would compromise the OSS installability story.

The boundary table at the top of this doc is the reference.

## Referenced work

- `docs/MEMORY_MODEL.md` — memory layers referenced by the state plane.
- `docs/MEMORY_BACKENDS.md` (PR #172) — pluggable memory backend
  architecture; drivers can serve per-tenant embedded stores today,
  shared managed stores later.
- `docs/ROADMAP_TO_PRODUCTION.md` — Milestone E sandboxing (deferred but
  design-relevant), section on multi-tenant / multi-machine gap
  (line 129) that this doc answers.
- `docs/MILESTONE_E_DESIGN.md` — pluggable-backend pattern that memory
  backends and (implicitly) tenant substrates follow.
- Hyperagent, Zep/Graphiti, Supabase, GitLab — reference open-core
  product shapes.

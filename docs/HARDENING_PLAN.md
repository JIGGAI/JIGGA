# Hardening Plan (H0–H3)

A consolidation + correctness pass on the existing runtime **before** new feature
milestones (E isolation / F distribution). The foundation is sound; the issues
below are concentrated and fixable. Source: the 2026-06 architecture audit.

Sequenced by **impact × risk-of-compounding**. H0 is a live bug; H1 are
scalability cliffs that get worse with data; H2 is decomposition that prevents
the next regression; H3 closes the largest doc↔code gap (team collaboration).

---

## H0 — Fix the dead `channels listen` CLI branch ✅

**Bug:** in `jigga/cli.py` the `channels listen` handler was orphaned *after* a
`return 0` inside the `approvals` command block, so `jigga channels listen` fell
through to a bare `return 0` and silently no-opped (a regression introduced when
the approvals/setup subcommands were added).

**Fix:** move the `listen` handler into the `channels` block before its bare
`return 0`; delete the dead copy. Add a CLI-routing test that asserts
`channels listen` actually invokes `channel_listen` (the unit tests of the
function didn't catch the *placement*).

---

## H1 — Scalability correctness

These are O(history) or unbounded-memory paths that are fine at demo scale and
get linearly worse as the audit log / task set / uptime grows.

### H1a — Running spend ledger (kill per-call full-log scan)
`model_router._budget_spent_before` → `cost.agent_spend` re-reads the **entire
audit log** (folding archives) on **every model call** to sum prior spend. At N
calls over a log of size L that's O(N·L).

**Fix:** maintain an append-cheap per-agent spend ledger under
`state/spend/<agent_id>.json` (`{window_start, spent}`), updated when a
`model.call` cost is recorded; budget check reads the ledger (O(1)) and only
falls back to a full scan to rebuild a missing/expired window. `cost_summary`
(reporting, not hot-path) can keep scanning. Keep the audit log as source of
truth; the ledger is a derived cache that can be rebuilt.

### H1b — Task index (kill O(all-tasks) per state change / tick)
`tasks.list_tasks` globs + parses **every** `*.json` on each call;
`tasks_for_agent`, `find_task`, `set_task_state` all route through it, and the
supervisor calls these every tick.

**Fix:** add `state/tasks/index.json` mapping `task_id → {state, assignee,
file}`; maintain it on `create_task`/`set_task_state`/`write_task`. Hot lookups
(`find_task`, `tasks_for_agent` for pending) consult the index; full
materialization stays available for reporting. Rebuild index from disk if
missing/stale.

### H1c — Bound daemon tick retention
`daemon.supervisor_loop` appends every tick result to an in-memory `ticks` list
forever (unbounded for an always-on process).

**Fix:** keep only the last K (e.g. 100) tick summaries in memory (ring buffer);
the audit log already has the durable record. Return the bounded tail.

### H1d — Dedup supervisor config loads
`supervisor._supervisor_tick` reloads runtime config / agents more than once per
tick. Load once per tick and pass down.

---

## H2 — Decomposition (prevent the next H0)

`cli.py` (~1k LOC, ~22-branch `if/elif main()`) and `dispatcher.py` (registry +
11 inline handlers) are god-objects; the H0 regression is a direct symptom.
Also: JSONL read/write is reimplemented 3× and `JiggaPaths` is exploded into
8-arg signatures.

### H2a — `core/io` JSONL helpers
Extract `read_jsonl` / `append_jsonl` / `rewrite_jsonl` into `jigga/core/io.py`;
replace the duplicates in `team_memory`, `memory_proposals`, `audit_query`
(reconcile their subtly-different skip-bad-line semantics into one).

### H2b — CLI command handlers
Move each `if args.command == "..."` body into a `handle_<command>(args)`
function in a `jigga/commands/` (or `jigga/cli_handlers/`) module, dispatched via
a `{command: handler}` dict mirroring `dispatcher.HANDLERS`. Add a routing test
per command asserting reachability.

### H2c — Dispatcher handlers package
Move the 11 inline handlers from `dispatcher.py` into
`jigga/runtime/handlers/`; keep the registry + `dispatch_action` as the spine.

### H2d — Thread `JiggaPaths`
Replace 8-arg path-list signatures (`run_team`, `run_workflow`, …) with the
existing `JiggaPaths` bundle.

---

## H3 — Team Runtime / enforced handoffs (close the thesis gap)

`team.py` (97 LOC) *logs* declared handoffs (`team.handoffs_declared`) but never
**acts** on them — the "teams of AI workers collaborating" promise is a stub.
This is the largest doc↔code gap and the headline value prop.

**Design (file-first / auditable — per the coordination decision):**
- Evaluate `routing.handoffs[].when` conditions against a run's task/output
  state; on match, create the next task assigned to `to`, recording the
  triggering `from` + condition on the task and in the audit log.
- A file-backed **decision log** (`workspaces/<team>/shared-context/handoffs.jsonl`)
  records each handoff: who, to whom, why, evidence path. No ephemeral bus.
- Keep it opt-in/bounded: a max-hops guard to prevent handoff loops (reuse the
  loop-prevention pattern).

Treated as a feature milestone (larger than H0–H2); land after the correctness +
decomposition passes so it's built on a firm base.

---

## Sequencing
1. **H0** (done) — live bug.
2. **H1a–d** — correctness cliffs; ship as one slice, each with a test.
3. **H2a–d** — decomposition; mechanical, test-guarded, can land incrementally.
4. **H3** — team runtime; its own milestone.

Hold Milestones E/F until H1 is merged.

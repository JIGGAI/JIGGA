# Review Fixes — 2026-05-29 (Handoff to Codex)

This document records changes made during a review pass after another AI shipped Phases 1–4. RJ asked for fixes to items #1–#5 and #8 from the review; items #6 (Elastic Delegation) and #7 (Capability Registry / structured tool dispatch) were intentionally deferred for Codex.

Status legend: ✅ done · 🟡 partial · ⏭️ deferred to Codex.

---

## Context

- Repo state at start of review: `a5588dd feat: add model execution router` (Phases 1–4 complete, 28 passing tests).
- Reviewer findings recorded in this conversation's summary; full ranked list lived in memory at `project_jigga_impl_status.md`.
- This file is the running notes Codex should read before continuing.

## Out of scope (held back, do these next)

- **#6 — Elastic Delegation / Subagents.** ~830 line spec at `docs/tools/ELASTIC_DELEGATION_SUBAGENTS.md`. Zero implementation. The `spawn_subagent` tool, `codex_cli`/`claude_code` adapters, session manager, and work-order templating are all unstarted. Held for Codex.
- **#7 — Capability registry + structured tool dispatch.** Spec at `docs/tools/CAPABILITY_REGISTRY_SKILL_PACKS.md`. Currently `jigga/runtime/workflow.py::_execute_step` is a hardcoded if/elif tree mapping action names to stub data. Replacing this with a capability-pack-driven registry is the natural next architectural seam and unlocks (#6) cleanly. Held for Codex.

## Changes (this pass)

### ✅ #1 — Filesystem deny matcher now matches nested paths

**Files:** `jigga/runtime/policy.py:45-78`, `tests/test_phase4_safety.py:35-65`

**Bug:** `_matches_any` for a deny entry like `.env` only matched the literal string `.env`. Nested paths like `apps/foo/.env` silently slipped through both the fnmatch check and the prefix check. Most security-sensitive denial pattern in the codebase.

**Fix:** Rewrote `_matches_any` + new `_path_matches` helper with three branches:
1. **Bare basename** (no `/`, no glob chars) → match if the pattern appears anywhere in `Path(raw).parts`. So `.env`, `id_rsa`, `secrets` now correctly match wherever they appear.
2. **`<prefix>/**` recursive glob** → find `<prefix>` as a consecutive subsequence in path parts with at least one segment below. So `secrets/**` matches `/workspace/secrets/key.pem` *and* `/anywhere/secrets/foo`. Absolute prefixes (`~/.gnupg/**`) only match at the root because their first part is `/`.
3. **Plain path** → exact or directory-prefix match (unchanged behavior).

**Tests added:**
- `test_filesystem_deny_matches_bare_basename_anywhere_in_tree` — covers nested `.env`/`id_rsa`/`secrets/**` plus false-positive guards (`foo.env.example`, `.envrc`).
- `test_filesystem_deny_handles_tilde_directory_patterns` — verifies `~/.ssh`/`~/.aws` deny still works, allow-listed `~/Projects` still allows.

**Behavior change for Codex to be aware of:** Users who deliberately wrote a deny entry of `.env` expecting it to only match a workspace-root file will now also deny nested `.env` files. This is the desired security behavior but technically a semantic change. None of the existing example configs ship with such patterns, so no migration is needed.

---

### ✅ #2 — Loop prevention: cron dedup + max_wakes_per_agent_per_hour

**Files:** `jigga/runtime/loop_guard.py` (new), `jigga/runtime/supervisor.py` (rewritten), `jigga/core/config.py:6-19` (new helpers), `tests/test_loop_guard.py` (new).

**Bug:** `config.yaml` shipped `supervisor.max_wakes_per_agent_per_hour: 12` but nothing read it. The scheduler emitted a `cron.tick` every supervisor tick whose minute matched the cron string, so two ticks within the same minute created two duplicate scheduled-wake tasks. No cooldowns, no rate limits — exactly what `ARCHITECTURE.md`'s "Loop Prevention" section warned against.

**Fix:**
- New module `jigga/runtime/loop_guard.py` with a tiny pure API: `load_loop_state` / `save_loop_state` / `cron_already_fired` / `record_cron_fire` / `should_skip_wake` / `record_wake` / `wake_count`. State persists at `~/.jigga/loop_state.json` as `{"wakes": {agent_id: [iso_ts, ...]}, "cron_fired": {"<target>|<cron>": "YYYY-MM-DDTHH:MM"}}`.
- New `core/config.load_runtime_config(home)` and `max_wakes_per_hour(home)` helpers (default 12, from `supervisor.max_wakes_per_agent_per_hour`).
- `supervisor.supervisor_tick` now:
  - dedupes cron events by `(target, cron, minute-bucket)` and `workflow.schedule_due` by `(workflow:id, schedule, minute-bucket)`,
  - throttles agent wakes when their hourly count is at or above `max_wakes_per_agent_per_hour`,
  - records every skip in the audit log (`supervisor.cron_deduplicated`, `supervisor.wake_throttled`),
  - returns `skipped_events` and `throttled` lists in its result dict so callers can see what was held back.

**Design decision:** The scheduler stays pure (deterministic from time). All dedup/throttle logic lives in the supervisor. This preserves the `due_events(at=...)` test interface and matches the doc separation ("Scheduler" emits, "Supervisor" decides to wake).

**Tests added (5):** cron dedup per-minute semantics; window-pruned wake count; supervisor throttles a saturated agent (task stays pending); two consecutive ticks don't double-create cron-wake tasks; loop state persists across loads.

**Note for Codex:** The throttled agent's task stays `pending`. The next supervisor tick within the same hour will throttle again. This is intentional — once the window expires, the supervisor picks the task back up. If you want immediate user feedback, surface `throttled` in the CLI output for `supervisor tick`.

---

### ✅ #4 — permission_mode axis on AgentConfig + policy

**Files:** `jigga/core/models.py:8-21,29-49`, `jigga/core/config.py:11-30`, `jigga/runtime/policy.py:11-19,57-79`, `jigga/runtime/workflow.py` (plan signature), `jigga/cli.py` (workflow plan call site), `tests/test_permission_mode.py` (new).

**Bug:** `docs/core/PERMISSION_MODES.md` defined a 5-state autonomy axis (plan_only / ask / accept_edits / autonomous / locked_down). Implementation had only per-resource modes (network: allow/ask/deny etc). `config.yaml` wrote `defaults.permission_mode: ask` but nothing consumed it.

**Fix:**
- Added `PermissionMode` `Literal` + `validate_permission_mode(value)` helper in `core/models.py`.
- Added `permission_mode: str | None = None` to `AgentConfig`; `from_dict` validates at load (invalid value raises `ValueError`).
- Added `core/config.default_permission_mode(home)` (reads `defaults.permission_mode` from `config.yaml`, falls back to `"ask"`).
- Added `runtime/policy.resolve_permission_mode(agent, default_mode)` to compute the effective mode.
- Added `runtime/policy.NON_EXECUTING_MODES = {"plan_only", "locked_down"}` and `APPROVAL_MODES = {"ask"}`.
- `evaluate_workflow_step` now takes `default_mode=...`. When the resolved mode is in `NON_EXECUTING_MODES`, the step is denied with permission `permission_mode.{mode}`. `ask` mode is intentionally NOT a uniform gate — the per-step `approval: required` flag + per-resource evaluators (filesystem/shell/network) provide finer control.
- `plan_workflow(workflow, agents, default_mode=...)` and `run_workflow` thread the default through. Plan output now includes `default_permission_mode` and each step's `policy.permission_mode`.

**Behavior:** Existing `ask`-mode workflows still run as before. `plan_only`/`locked_down` agents now uniformly block their workflow steps. CLI `workflow plan` shows the active mode.

**Tests added (9):** validator accepts/rejects, agent config validates at load, default reader, mode resolver fallback, plan blocked under plan_only, run returns blocked under locked_down, mode constants documented, existing workflow still runnable under default ask, plan surfaces default mode.

---

### ✅ #3 — Wire policy + permission_mode into run_agent

**Files:** `jigga/runtime/agent.py` (rewritten), `tests/test_permission_mode.py:75-103` (2 new tests).

**Bug:** `run_agent` never consulted the policy layer. Today's blast radius was small (only side effects: model call + JSON artifact write under `runs/`), but the doc treats policy as the centerpiece of safety. No `policy.evaluated` audit either.

**Fix:**
- On every agent run, resolve the effective permission mode and emit a `policy.evaluated` audit event with `permission_mode`, `runtime_default`, and `agent_override` fields.
- If the mode is `plan_only` or `locked_down`: skip the model call, mark each pending task as `needs_approval`, emit `policy.denied` audit per task, and return a record with `status: "policy_denied"` containing a `held_tasks` list.
- Other modes (`ask`, `accept_edits`, `autonomous`) proceed normally — the artifact JSON now also records the effective mode.
- The `agent.run.started` and `agent.run.completed` audits carry `permission_mode` for downstream observability.

**Conservative scope:** This is the agent-run boundary only. Per-action enforcement (shell allow/deny on every tool call, network domain checks) is still pending — but the evaluators in `policy.py` already exist for `safe_process` and `workflow.plan_workflow`, and Codex can extend them when real tool dispatch lands.

**Note for Codex:** A `plan_only` agent's tasks accumulate as `needs_approval` and never auto-clear. There's no UI yet to approve them. Easiest interim path: a `jigga task approve <id>` CLI subcommand that moves a task back to `pending` and bumps a per-task `approval_granted_at` field. Beyond that, the approval flow likely deserves its own design pass (probably ties into the channel-gateway notion).

---

### ✅ #5 — Workflow inference detects multi-step session shapes

**Files:** `jigga/runtime/inference.py` (rewritten), `tests/test_inference_depth.py` (new).

**Bug:** Inference counted only `(agent_id, title)` pairs. `WORKFLOW_INFERENCE.md` listed `repeated_tool_sequence` / `repeated_agent_chain` / `recurring_time_pattern` as expected signals; none were detected. Suggested workflows were always one-step stubs.

**Fix — dual-signal architecture:**
1. **Signal A (new): multi-step session shapes.** Group completed events into time-windowed *sessions* (default 5 min gap). Collapse consecutive identical events. Multi-step shapes (length >= 2) recurring `min_count` times produce multi-step workflow suggestions.
2. **Signal B (preserved): single-action repetition.** Count `(agent, title)` occurrences across all candidate events; produce one-step suggestion when count >= min. Keys already covered by a multi-step suggestion are suppressed to avoid noise.
3. **Time pattern hint:** when sessions cluster around a modal hour (majority concentration), the suggestion gets `modal_hour_utc` + a `hint` string. The hint is advisory only — the trigger itself remains `{"type": "manual"}` so the user explicitly opts in.
4. **`apply_suggestion` no longer crashes on re-apply.** Returns `{"status": "already_applied", ...}` when the target YAML already exists, instead of the unhandled `FileExistsError`.

**Behavior changes:**
- Suggestion step IDs are now `step_1`, `step_2`, ... (was `run_inferred_task`). Existing applied workflows aren't affected — apply just writes a file once.
- `suggestion["step_count"]` and `suggestion["modal_hour_utc"]` are new fields. `suggestion["hint"]` appears conditionally.
- Sessions are bounded by event timestamps. Events without parseable timestamps are dropped from the session grouping (but still counted in Signal B).

**Tests added (4):** 3-step shape detected across 3 sessions with hour-7 cluster; repeated identical events collapse to one step; time-pattern hint suppressed when sessions don't cluster; `apply_suggestion` returns `already_applied` on re-apply.

**Note for Codex:** Session gap is a fixed 5 minutes. Consider making it configurable via `~/.jigga/config.yaml` under `inference.session_gap_minutes`. Also: file-path repetition and prompt-similarity signals from `WORKFLOW_INFERENCE.md` still aren't implemented — those need event types that don't exist yet.

---

### ✅ #8 — Small-bugs batch

#### (a) `apply_suggestion` returns `already_applied` on re-apply
Already covered as part of #5. `inference.apply_suggestion` now checks for the target file and returns `{"status": "already_applied", ...}` instead of raising an unhandled `FileExistsError`.

#### (b) Generalized `_friendly_schedule_due`
**Files:** `jigga/runtime/scheduler.py:1-35,50-72`, `tests/test_small_bugs.py:14-32`.

**Was:** literal-string detection — only matched "weekday" + "7:30" / "07:30". Anything else silently never fired.

**Now:** a `_TIME_PATTERN` regex + `_parse_friendly_time` helper handle `HH:MM`, `HH:MM am|pm`, `Ham|pm`, with 12-hour conversion. Day-of-week filters recognize `weekday`/`weekdays`, `weekend`, and `daily`/`every day` (no filter). Returns `False` cleanly on unparseable input.

**Cron strings remain unaffected** — they still go through the 5-field `_cron_due` path.

**Tests added (2):** parser covers all common forms; due-check honors day-of-week filter and time precision.

#### (c) Atomic JSON/YAML writes
**Files:** `jigga/core/io.py:14-39`, `tests/test_small_bugs.py:35-43`.

**Was:** `write_json` / `write_yaml` did a direct `path.write_text`. Concurrent readers could see truncated/partial files; a crash mid-write left a corrupted file on disk.

**Now:** new `_atomic_write_text(path, content)` writes to `<path>.tmp` and `os.replace`s — atomic on POSIX. All `write_json` / `write_yaml` callers (state.json, loop_state.json, every run.json, every suggested-workflow YAML) inherit the safety for free.

**Tests added (1):** verifies no leftover `.tmp` sibling after writes, content survives a rewrite.

#### (d) Supervisor loop signal handler
**Files:** `jigga/runtime/daemon.py` (rewritten), `tests/test_small_bugs.py:53-83`.

**Was:** `time.sleep(interval_seconds)` with no signal handling. Ctrl-C worked by raising `KeyboardInterrupt` mid-sleep, but there was no clean shutdown path, no audit of why the loop stopped, and no graceful response to SIGTERM (matters for `systemd`/`supervisord` deployments later).

**Now:** registers SIGINT + SIGTERM handlers (only if running on the main thread — `signal.signal` raises `ValueError` otherwise; the fallback is silently skipped so test contexts work). On signal, the handler sets a stop flag; the loop notices on its next check and exits cleanly. Return dict adds `stopped_by_signal` (signal number or `None`); `status` is `"stopped"` for natural completion vs `"interrupted"` for signal.

**Tests added (1):** drives a real subprocess (matches CLI use), sends SIGTERM, asserts `status="interrupted"`, `stopped_by_signal=15`, at least one tick completed.

#### (e) Team runtime surfaces handoffs
**Files:** `jigga/runtime/team.py` (rewritten), `tests/test_small_bugs.py:85-110`.

**Was:** `team.routing.handoffs` declared in YAML was completely ignored — not on the coordination task, not in the audit log, not in the return record.

**Now (still a "skeleton" but observable):**
- On run start, emit `team.handoffs_declared` audit event with the full handoffs list.
- Coordination task metadata carries `handoffs` so a downstream agent or future routing layer can consume it.
- The team-run return record includes `handoffs` at the top level.
- `team.run.started` audit now includes `handoff_count`.

**Tests added (1):** verifies social_content_team's two handoffs appear in metadata, audit, and the return record.

**Note for Codex:** Actual conditional handoff evaluation (the `when:` clauses) is still unimplemented. That's the natural next layer once the capability registry exists — `when` predicates probably need to be tool-callable or attribute lookups against task state.

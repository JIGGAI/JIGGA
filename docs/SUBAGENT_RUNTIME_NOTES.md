# Subagent Runtime Notes

This note records the first controlled elastic-delegation implementation.

## What changed

- Added a `spawn_subagent` capability via the existing capability registry.
- Added `jigga/runtime/subagents.py` with:
  - work-order validation
  - session persistence under `~/.jigga/sessions/<session_id>/session.json`
  - dry-run backend
  - gated `codex_cli` backend
  - delegation policy checks
  - session list/inspect/cancel helpers
- Added `delegation` config support on agents.
- Added global `delegation_policy` defaults in `jigga init`.
- Added CLI commands:
  - `jigga sessions list`
  - `jigga sessions inspect <session_id>`
  - `jigga sessions cancel <session_id>`
- Added workflow dispatch integration so a workflow step can call `spawn_subagent`.

## Safety behavior

- Delegation is denied unless the executing agent explicitly enables it.
- Backends must be on the agent/global allowlist.
- `codex_cli` requires `delegation_policy.codex_cli_enabled: true` in runtime config.
- Depth and per-parent parallelism limits are enforced before session creation.
- Sessions are audited with `subagent.spawn.planned`, `subagent.spawn.started`, `subagent.spawn.completed`, and `subagent.spawn.failed`.
- The initial example agent enables only the `dry_run` backend.

## Follow-up work

- Add real process lifecycle tracking for long-running asynchronous adapters.
- Add stronger sandboxing/environment shaping for `codex_cli` before enabling it by default.
- Add parent-review gates before accepting subagent artifacts.
- Add aggregation helpers for multiple subagent results.

# Subagent Runtime Notes

This note records the first controlled elastic-delegation implementation.

## What changed

- Added a `spawn_subagent` capability via the existing capability registry.
- Added `jigga/runtime/subagents.py` with:
  - work-order validation
  - session persistence under `~/.jigga/sessions/<session_id>/session.json`
  - dry-run backend
  - gated `codex_cli` backend (`codex exec <prompt>`)
  - gated `claude_code` backend (`claude --print <prompt>`)
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
- Subagent `cwd` and declared filesystem permissions must fit inside the parent agent filesystem policy.
- `codex_cli` and `claude_code` run with a restricted environment allowlist (PATH/HOME/LANG/LC_ALL/TERM only); secrets are not inherited unless explicitly requested via the work-order's `permissions.secrets.required`.
- `codex_cli` requires `delegation_policy.codex_cli_enabled: true` in runtime config.
- `claude_code` requires `delegation_policy.claude_code_enabled: true` in runtime config.
- Both external CLI backends use the shared `runtime.sandbox` primitive — env allowlist + cwd + timeout in one place — so adding another external CLI backend (or eventually plugging in OS-level sandboxing) is a one-file change.
- Depth and per-parent parallelism limits are enforced before session creation.
- Sessions are audited with `subagent.spawn.planned`, `subagent.spawn.started`, `subagent.spawn.completed`, and `subagent.spawn.failed`.
- The initial example agent enables only the `dry_run` backend.

## Follow-up work

- Add real process lifecycle tracking for long-running asynchronous adapters.
- Add stronger OS/process sandboxing (firejail / bwrap / container) behind `runtime.sandbox.run_sandboxed` before enabling the external CLI backends by default.
- Add parent-review gates before accepting subagent artifacts.
- Add aggregation helpers for multiple subagent results.

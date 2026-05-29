# Review Fixes — PR #5 (2026-05-29)

Follow-up PR after #2/#3/#4 cleared review and merged. Picks up the gaps that were either deferred at the time, partial in scope, or discovered while reviewing the previous fix commits.

Status legend: ✅ done · 🟡 partial · ⏭️ deferred.

## Summary of changes

7 commits, ~600 LOC of impl + ~280 LOC of tests, 100 passing tests (was 84 on main at branch-off).

### ✅ Subagent deny-narrowing fix (PR #4 follow-up)

**Files:** `jigga/runtime/subagents.py:171-184`, `tests/test_subagents.py:170-185`.

**Bug discovered while reviewing PR #4's fix commit (`9631291`):** the new policy check rejected exactly the case where a subagent voluntarily narrows scope below the parent's allow — i.e. it disabled the canonical "stricter than parent" pattern that the spec explicitly endorses. Removed the wrong check; the existing read/write-must-fit-parent check still enforces that subagents cannot *exceed* parent reach. Test renamed and inverted to assert that deny narrowing is permitted.

### ✅ Drop content-drafting risk_level to low

**File:** `jigga/runtime/capabilities.py:137`, `tests/test_capabilities.py:146-174`.

The bundled `content-drafting` capability shipped as `medium`, which made the `social_content_syndication` demo workflow block at every step under default `ask` mode. Dropped to `low` so the demo runs cleanly. The risk-level approval gate is still covered by `test_medium_risk_capability_requires_approval_under_ask_mode`, which now uses a synthetic medium capability via user-local manifest.

### ✅ Memory/runtime context separation in dispatcher

**Files:** `jigga/runtime/dispatcher.py` (rewritten), `jigga/runtime/workflow.py:127-138`, `tests/test_capabilities.py` (new leak test).

The dispatcher used to pass a single dict carrying both memory context (from `build_context_package`) and runtime plumbing (`home`, `logs_dir`, `sessions_dir`, `agent`). `_summarization_handler` had to manually filter the runtime keys out of its output. New `RuntimeContext` dataclass + revised handler signature `(step, capability, resolved_input, memory_context, runtime)` — handlers receive both separately. New test pins the non-leak property.

### ✅ Broader capability permission enforcement (PR #3 #1)

**Files:** `jigga/runtime/policy.py:142-200` (new `evaluate_resource_permission`), `jigga/runtime/dispatcher.py:32-70` (extended `evaluate_capability_permissions`), `tests/test_capabilities.py:101-160` (new tests).

The old `evaluate_capability_permissions` only walked `permissions.filesystem.read/write`. Bundled capabilities also declare `{calendar: read}`, `{email: read}`, `{notifications: send}`, `{memory: read}` — those were unenforced metadata.

- Added `policy.evaluate_resource_permission(agent, resource, required)` — a generic evaluator for flat scalar permissions.
- Extended `dispatcher.evaluate_capability_permissions` to walk filesystem (existing), network (mode-based), calendar/email/notifications (scalar), and memory (special-cased to require `agent.memory_scope`).
- Delegation continues to be enforced inside `spawn_subagent` (where `agent.delegation.enabled` lives), not at the capability boundary.

### ✅ Extensible handler dispatch (PR #3 #4)

**Files:** `jigga/runtime/dispatcher.py:135-180`, `tests/fixtures/{__init__.py,capability_handlers.py}` (new), `tests/test_capabilities.py:177-218` (new tests).

`HANDLERS` was a fixed dict in `dispatcher.py`. Adding a new capability required touching the dispatcher. New `resolve_handler(name)` checks the built-in dict first; if the name is a dotted `module.path:function` reference, imports it lazily (LRU-cached). Built-in handlers continue to use their short string keys.

**Trust boundary note:** the import target is fully under user control via the manifest. First-use approval is what gates trust — this commit doesn't validate that the imported callable is safe; the next one does (via the scanner).

### ✅ First-use approval for user-local capability packs (PR #3 #2)

**Files:** `jigga/runtime/capabilities.py:175-220` (new helpers + extended `load`), `jigga/runtime/workflow.py:102-108` (passes `approvals_dir`), `jigga/cli.py:70-86,222-258` (new CLI), `tests/test_capabilities.py:280-360` (new tests).

User-local manifests now require a recorded approval before the registry dispatches through them. Approvals live at `~/.jigga/policies/capability_approvals.json` keyed by `name + manifest_hash`.

- `CapabilityRegistry.load` gains an optional `approvals_dir` kwarg. Without it, behavior is unchanged (no gating — preserves all existing test call sites).
- `workflow.run_workflow` and the CLI `capabilities` subcommand always pass `paths.policies`.
- Unapproved or hash-mismatch manifests land in `registry.pending` and are not dispatched. `resolve_action` only walks active capabilities.
- New CLI: `jigga capabilities approve <path> [--approve]` records approval; `jigga capabilities pending` lists pending packs.
- Bundled capabilities are never gated.
- Manifest hash change (e.g. version bump) invalidates the approval until re-approved — tested via `test_approval_invalidated_by_manifest_hash_change`.

### ✅ Capability manifest security scanner (PR #3 #3)

**Files:** `jigga/runtime/capability_scanner.py` (new, ~190 LOC), `jigga/cli.py:222-282` (wired into `validate` and `approve`), `tests/test_capability_scanner.py` (new, 9 tests).

Static scanner that surfaces a structured risk report before approval. Categorizes findings by severity (`low`/`medium`/`high`) and code. Detects:

- **Broad filesystem access** (`/`, `~`, `~/**`, `**`) → high.
- **Sensitive path tokens** (`.ssh`, `.aws`, `.gnupg`, `keychain`, `wallet`, `id_rsa`, `id_ed25519`, `.env`, `secrets`, `credentials`) anywhere in declared paths → high.
- **Suspicious handler imports** (`os:`, `subprocess:`, `shutil:`, `ctypes:`, `importlib:`, `builtins:`) → high.
- **Remote-script install patterns** (`curl|wget … | sh|bash|zsh`, the `curl -fsSL … | sh` shape) in pack scripts → high.
- **Post-install hook filenames** (`install.sh`, `postinstall.sh`, `setup.sh`) → medium (informational).
- **Shell/network/secrets declarations** → medium/high.

Wired into:
- `jigga capabilities validate <path>` — output now includes the scan report.
- `jigga capabilities approve <path>` (dry-run mode) — shows the scan before the user records approval. Findings are advisory; the user can still approve a flagged pack after review.

## Held / not addressed in this PR

- The memory-context split is internal; no migration for existing run artifacts on disk (they were never persisted in a way that depended on the merged shape).
- **Network domain allowlist** at the capability-permissions level — the spec calls for `permissions.network.allow_domains` per capability; the current evaluator just checks the agent's network mode. Worth a follow-up once any real network adapter ships.
- **`bundled` flag on manifests** is still surfaced but only used to gate approval. UI hooks (the original "what's it for" question) — still TBD when there's a UI.
- **Project-local capabilities** — already in scan logic via `project_capabilities` kwarg but no CLI/path support yet. Doc lists as follow-up.
- **Network/shell/secrets evaluators called from `evaluate_capability_permissions`** are minimal — they reuse existing `evaluate_network` / `evaluate_shell` and treat the capability declaration as a probe. Refinement (per-capability target domains, per-capability shell commands) can come with the first real handler that needs them.

## Test coverage delta

Before this PR (main HEAD): 84 passing
After this PR: 100 passing (+16 tests)

New test files:
- `tests/test_capability_scanner.py` — 9 tests covering each finding category + CLI surfacing.
- `tests/fixtures/{__init__.py, capability_handlers.py}` — fixture module for the extensible handler test.

Existing files updated:
- `tests/test_subagents.py` — deny-narrowing test renamed and inverted.
- `tests/test_capabilities.py` — synthetic medium-risk capability for the gating test; new tests for context leak, broader permissions, dotted-handler resolution, first-use approval, hash-drift invalidation, CLI approve flow.

## Notes for next maintainer

1. The dispatcher's `evaluate_capability_permissions` is becoming the natural entry point for permission integration. When a new resource type lands (browser, calendar write, etc.), add an evaluator in `policy.py` and call it from the capability boundary.
2. The scanner is intentionally conservative — it surfaces findings but doesn't auto-reject. As real adapters ship, consider a `--fail-on-high` flag for the validate command in CI contexts.
3. First-use approval is hash-pinned. If you ever support "update this pack and re-approve in one step," do it via a `--update` flag on the approve command rather than auto-promoting; the auto-promote path is exactly what attackers want.

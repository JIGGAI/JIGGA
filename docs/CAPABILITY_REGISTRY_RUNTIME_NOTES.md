# Capability Registry Runtime Notes

Implemented after the 2026-05-29 runtime review fixes as the next architectural seam for workflow execution.

## What changed

- Added a local capability registry in `jigga/runtime/capabilities.py`.
- Added a structured workflow dispatcher in `jigga/runtime/dispatcher.py`.
- Added `~/.jigga/capabilities` to runtime paths and `jigga init` directory creation.
- Added CLI inspection commands:
  - `jigga capabilities list`
  - `jigga capabilities inspect <name>`
  - `jigga capabilities validate <path>`
- Updated workflow planning to surface capability and risk metadata per step.
- Updated workflow execution to resolve actions through the registry before dispatch.
- Unknown actions now block cleanly during planning instead of falling through to an implicit dry-run stub.

## Current registry behavior

Precedence is:

1. User-local packs under `~/.jigga/capabilities/**/manifest.yaml`
2. Bundled runtime capabilities

Bundled capabilities currently cover the MVP demo actions:

- `calendar`
- `email`
- `notifications`
- `summarization`
- `content-drafting`

Most handlers are real adapters now (notifications, filesystem, Google Calendar, gog/Workspace, `draft_with_model`, MCP servers, subagents, channels); **email remains the dry-run stub**. Action resolution, plan metadata, audit events, and failure behavior all go through the capability seam. Bundled/user-local capability metadata includes `bundled`, `handler`, and `manifest_hash` fields so future UI and approval flows can distinguish built-in packs from user-installed packs and detect manifest changes.

## Safety gates in this slice

- Symlinked manifests are rejected.
- User-local manifest SHA-256 hashes are recorded in registry output.
- Medium/high risk capabilities require approval unless the effective agent permission mode is `autonomous`.
- Declared filesystem permissions are checked against the executing agent's filesystem policy before workflow steps run.

## Follow-up work

- ✅ ~~Add project-local capability paths~~ (PR #10; `docs/PROJECT_LOCAL_CAPABILITIES_RUNTIME_NOTES.md`).
- ✅ ~~Add capability install/plan/apply flow and persistent first-use approval records~~ (`jigga capabilities install/uninstall/approve/pending`; approvals at `policies/capability_approvals.json`).
- Move built-in capability manifests to package data files once the manifest shape stabilizes. *(still open)*
- ✅ ~~Replace dry-run handlers with real adapters~~ (all but email — see above).
- ✅ ~~Build elastic delegation as a `spawn_subagent` capability~~ (`runtime/subagents.py`: dry_run/codex_cli/claude_code backends; `docs/SUBAGENT_RUNTIME_NOTES.md`).

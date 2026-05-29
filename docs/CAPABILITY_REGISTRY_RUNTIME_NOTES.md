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

Handlers are still dry-run implementations, but action resolution, plan metadata, audit events, and failure behavior now go through the capability seam.

## Follow-up work

- Add project-local capability paths.
- Add capability install/plan/apply flow.
- Move built-in capability manifests to package data files once the manifest shape stabilizes.
- Replace dry-run handlers with real adapters behind the same dispatcher contract.
- Build elastic delegation as a `spawn_subagent` capability/tool on top of this registry.

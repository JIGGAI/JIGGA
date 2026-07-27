# Project-Local Capabilities Runtime Notes

Second Milestone A slice. Lets a workspace ship its own capability packs under `<project>/.jigga/capabilities/` without polluting the user-global `~/.jigga/`.

## What changed

- New helpers in `jigga/core/paths.py`:
  - `resolve_project_root(project=None)` — resolution order: explicit `project` argument > `JIGGA_PROJECT` env var > `None`. **Explicit-only since 2026-06-07**: the original gitignore-style cwd-walk auto-discovery was removed after it crossed out of the repo, found the user's real `~/.jigga`, and declared `$HOME` a project — silently bleeding runtime state into contexts that never opted in.
  - `project_capabilities_dir(project_root)` — returns `<project_root>/.jigga/capabilities` (whether or not it exists; `scan_capability_dir` already handles missing directories gracefully).
- New top-level CLI flag: `--project <dir>`. Threaded into every call site that constructs a `CapabilityRegistry` (workflow plan, workflow run, capabilities list/inspect/pending/approve/validate).
- `run_workflow` gained a `project_capabilities: Path | None` kwarg; the CLI passes it through.

## Precedence

Per `CapabilityRegistry.load`, the load order is:

1. **Project-local packs** (highest) — registered first, win any action-name collision.
2. **User-local packs** — registered next.
3. **Bundled capabilities** — registered last.

Same approval mechanism applies to project packs as to user packs: unapproved manifests land in `registry.pending` and are not dispatched until `jigga capabilities approve <manifest>` records the approval. Bundled capabilities are never gated. The approval index lives at `~/.jigga/policies/capability_approvals.json` regardless of where the manifest came from — keyed by `name + manifest_hash` semantics.

## Project root resolution order

When the CLI is invoked, the project root is resolved like this:

| Priority | Source | Example |
|---|---|---|
| 1 | Explicit `--project <dir>` CLI flag | `jigga --project ~/code/my-content workflow run ...` |
| 2 | `JIGGA_PROJECT` env var | `JIGGA_PROJECT=~/code/my-content jigga ...` |
| 3 | None — no project capabilities loaded | `jigga workflow run hello` |

Mental model: project capabilities are an opt-in extension — opting in is saying `--project` (or exporting `JIGGA_PROJECT` in the project's direnv/shell). There is deliberately no cwd-based auto-detection: a folder you merely stand in must not be able to add verbs to your agents.

## Quick smoke

```bash
# Create a project workspace with a capability pack
mkdir -p /tmp/myproject/.jigga/capabilities/my-cap
cat > /tmp/myproject/.jigga/capabilities/my-cap/manifest.yaml <<EOF
name: my-cap
version: 1.0.0
summary: Project-local capability
actions:
  - mycap.run
risk_level: low
EOF

# From outside the project: capability is not visible
jigga --home /tmp/demo capabilities list | grep mycap.run   # (empty)

# With the project named explicitly (auto-detect was removed in PR #126 —
# project root is explicit-only: --project or JIGGA_PROJECT):
jigga --home /tmp/demo --project /tmp/myproject capabilities pending   # shows my-cap

# Approve and use
jigga --home /tmp/demo capabilities approve \
    /tmp/myproject/.jigga/capabilities/my-cap/manifest.yaml --approve
jigga --home /tmp/demo capabilities list | grep mycap.run   # now mapped to my-cap
```

## Follow-up work

- Project-local agents (`<project>/.jigga/agents/`) — the registry pattern is now in place; agents/workflows can follow the same auto-detect approach.
- Project-local workflows (`<project>/.jigga/workflows/`) — same.
- Project-local `settings.yaml` for overrides (e.g. `default_permission_mode: ask` per project).
- A `.jigga/` skeleton scaffold command (`jigga project init`) once we know what additional files belong there.

## Constraints / known edges

- The approval index keys by capability `name`. Two different projects shipping a capability with the same `name` but different `manifest_hash` will require re-approval each time you switch projects (the hash mismatch falls back to pending). For workflows that need cross-project name reuse, this is mild friction; if it becomes painful, change the key shape to `(name, manifest_hash)` so both versions can be approved simultaneously.
- ~~Auto-detect walks the full filesystem path up to `/`~~ — **removed (PR #126)**: cwd-walk auto-discovery reached the real `~/.jigga` from repo checkouts and bled runtime state into isolated contexts. The project root is now explicit-only (`--project <path>` / `JIGGA_PROJECT`), as noted at the top of this doc.

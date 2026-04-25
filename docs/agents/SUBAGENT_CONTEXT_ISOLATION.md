# Subagent Context Isolation

Subagents are temporary workers spawned by a primary agent through elastic delegation. They should have isolated context and bounded permissions.

## Principle

A subagent should receive a work order, not the parent agent's entire memory or authority.

```text
Primary Agent
  -> creates bounded work order
  -> spawns subagent session
  -> subagent returns artifacts/results
  -> primary agent reviews and merges
```

## Required Work Order Fields

```yaml
subagent_task:
  id: subtask_001
  parent_agent: engineer
  backend: codex_cli
  goal: Write tests for auth/session handling.
  cwd: ./apps/api
  memory_scope: project_view_minimal
  files:
    allow:
      - apps/api/src/auth/**
      - apps/api/tests/auth/**
    deny:
      - .env
      - secrets/**
  output_required:
    - summary
    - changed_files
    - test_results
    - risks
```

## Isolation Rules

- Subagents do not write directly to shared long-term memory.
- Subagents return structured results to the parent.
- Parent agents decide what becomes memory.
- Subagents cannot spawn subagents in v1.
- Subagents should run with stricter permissions than parent agents.

## Recommended Defaults

```yaml
delegation_policy:
  max_depth: 1
  max_parallel_subagents: 4
  require_parent_review: true
  subagents_can_spawn_subagents: false
  default_permission_mode: locked_down
```

## Backends

Initial compatible execution backends:

- Codex CLI
- Codex cloud task runner
- Claude Code
- local model worker
- generic shell-based agent adapter

Each backend should be wrapped by JIGGA's session manager and policy layer.

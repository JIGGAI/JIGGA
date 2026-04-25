# Project Directory & Local AI Layer

JIGGA should support a project-local directory that makes each repository portable, configurable, and inspectable by humans.

## Recommended Directory

```text
.jigga/
  settings.yaml          # project-local runtime settings
  agents/                # project-specific agent definitions
  skills/                # project-specific capability packs
  rules/                 # path-scoped rules and coding/content conventions
  hooks/                 # lifecycle hooks
  workflows/             # reusable project playbooks
  memory/                # project-scoped memory indexes and summaries
```

## Why This Matters

The `.jigga/` directory turns a normal repo into an AI-operable workspace. Agents can discover the project's expectations without relying on hidden prompts or centralized cloud state.

This borrows the strongest pattern from modern agentic coding tools: keep instructions, settings, subagents, hooks, and rules close to the work.

## Precedence Model

JIGGA should resolve configuration from broad to narrow:

```text
managed policy
  > user policy
  > project settings
  > team settings
  > agent settings
  > workflow/task overrides
```

Higher-precedence policy can restrict lower-precedence configuration, but lower-precedence configuration should not be able to grant itself more power than policy allows.

## Example `.jigga/settings.yaml`

```yaml
project:
  name: example-app
  default_memory_scope: project_view

runtime:
  permission_mode: ask
  max_parallel_agents: 4
  default_model: gpt-5.5

paths:
  workspace_root: .
  instructions: ./JIGGA.md
  local_instructions: ./JIGGA.local.md

security:
  deny_paths:
    - .env
    - .ssh/**
    - secrets/**
  allow_shell: false
```

## Implementation Notes

- `.jigga/` should be version-controlled by default except private memory and local settings.
- `.jigga/memory/` may contain generated summaries and indexes; allow projects to choose whether to commit summaries.
- `JIGGA.local.md` should be gitignored and used for private user notes.

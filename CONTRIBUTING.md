# Contributing to JIGGA

JIGGA is currently in product-definition and architecture planning stage.

## How to Contribute

Useful contributions right now:

- refine architecture docs
- improve YAML schemas
- add example agents, teams, workflows, and memory scopes
- propose MVP implementation approaches
- identify safety and sandboxing risks
- prototype CLI/runtime components

## Design Rules

1. Prefer local-first behavior.
2. Prefer declarative configuration.
3. Treat memory as scoped and permissioned.
4. Do not add autonomous behavior without approval gates.
5. Avoid raw shell access where structured tools are possible.
6. Keep workflows as reusable playbooks, not a mandatory central engine.

## Naming

Core nouns should remain consistent:

- Agent
- Team
- Workflow
- Task
- Memory Scope
- Policy
- Supervisor
- Runtime

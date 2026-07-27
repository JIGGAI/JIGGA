# Contributing to JIGGA

JIGGA is a working local-first runtime — 900+ passing tests across the
supervisor, agent/team runtimes, workflow engines, scoped memory, channels,
model routing, and observability — installable from source or PyPI. See
[`docs/ROADMAP_TO_PRODUCTION.md`](docs/ROADMAP_TO_PRODUCTION.md) for what's
open on the road to v1.0.

## How to Contribute

Useful contributions right now:

- pick up an open roadmap item or GitHub issue (isolation/sandboxing, email
  connector, new channels, workflow media nodes)
- add or improve recipes (agents + teams + workflows that work out of the box)
- build capability packs (new actions behind the policy/approval gates)
- add tests — especially around failure paths and policy gating
- improve docs where they've drifted from behavior
- identify safety and sandboxing risks

Run the suite with `make test`; lint with `ruff`. Tests must never touch the
real system (stub side-effecting calls — see `tests/conftest.py`). Keep the
core dependency-free beyond PyYAML; opt-in capabilities and the UI may bring
their own.

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

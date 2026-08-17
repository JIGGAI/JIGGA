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

Run the suite with `make test`; lint with `ruff`; `make check` does both, and
`make coverage` runs what CI enforces. Tests must never touch the real system
(stub side-effecting calls — see `tests/conftest.py`). Keep the core
dependency-free beyond PyYAML; opt-in capabilities and the UI may bring their
own.

## CI

Every pull request runs [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

- the suite + `ruff` on **Python 3.11, 3.12, and 3.13** — every version
  `pyproject.toml` advertises, since 3.11 is the floor a fresh install may get
- **coverage with an 85% floor.** It is a collapse detector, not a target to
  grind upward
- **a packaging smoke test** — build the wheel, install it into a clean venv,
  and assert `jigga init --examples` still finds the bundled recipes. The
  `examples/` → `jigga/examples` force-include is easy to break invisibly
- **a tests-required gate**: a PR touching `jigga/` must also touch `tests/`.
  Maintainers can bypass it with the `no-tests-ok` label for docs-only
  refactors, pure renames, and reverts

`ruff` is pinned to one version in both CI and `make dev`, and the enabled
rules are listed in `pyproject.toml` under `[tool.ruff.lint]`. An unpinned
linter changes the build's verdict when a new version ships with no commit to
blame — ruff 0.16 turned on isort, pyupgrade, and flake8-datetimez by default,
which took a clean tree to 289 findings. Adopting more rules is welcome, as its
own PR with the fixes in it.

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

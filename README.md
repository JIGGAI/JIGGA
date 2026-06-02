# JIGGA

**JIGGA is a local-first operating system for personal AI workers.**

It is designed as a Terraform-style system for declaring, running, and coordinating persistent AI agents, agent teams, reusable workflows, and shared memory on a user's own machine.

> Infrastructure-as-code for personal AI workers.

## What JIGGA Is

JIGGA lets users define:

- **Agents** — individual AI workers with roles, tools, permissions, and memory scopes.
- **Teams** — reusable groups of agents that collaborate on categories of work.
- **Workflows** — declarative playbooks/SOPs that agents can invoke repeatedly.
- **Tasks** — units of work that agents can create, claim, delegate, and complete.
- **Memory** — local, file-first, persistent context shared across agents through scoped views.
- **Policies** — explicit permissions governing memory, filesystem, shell, network, and tool access.

JIGGA is not intended to be just a chatbot, a one-off automation tool, or a prompt framework. It is a runtime and configuration layer for AI workers that feel persistent because their tasks, memory, state, and workflows persist.

## What Sets JIGGA Apart

- **vs. personal-assistant runtimes (e.g. OpenClaw):** adds **declarative teams**, **scoped memory**, **repeatable workflows**, **agents-as-code recipes**, and a **plan/diff before applying** config changes.
- **vs. memory-agent assistants (e.g. Hermes):** makes memory **scoping, permissions, and approval explicit** — and **file-first/auditable** (every action lands in the audit log; every coordination decision is a file you can read, not an ephemeral message bus).
- **vs. agent SDKs/frameworks (e.g. LangChain):** JIGGA is a **runtime + config layer (an "OS")**, not a library you wire into an app. You declare workers in files; the supervisor runs them.
- **vs. cloud agent platforms:** **local-first** — your memory, state, logs, and credentials stay on your machine. **Bring your own model/credentials**; JIGGA-the-project never holds a shared key.

Three things make it distinctive in practice:

1. **Run on subscriptions and your local CLIs, not metered API keys.** The model router supports a **ChatGPT-subscription provider via OAuth** (plus any OpenAI-compatible endpoint), and agents can **delegate to your locally-installed `codex` and `claude` CLIs** as subagent backends — executing those tools directly with their own login, not a per-token API key (`jigga auth login codex_cli|claude_code`). A team can *think* without metered API costs.
2. **Auditable by construction.** A single trace id threads a whole operation (supervisor tick → agent run → tool call → subagent); `jigga trace <id>` reconstructs it. Coordination (handoffs, decisions, memory) is files under `~/.jigga/`.
3. **Repeatable work becomes infrastructure — and JIGGA proposes it.** Beyond declaring workflows by hand, JIGGA watches your audit log for recurring multi-step patterns and **suggests turning them into reusable, schedulable workflows** (`jigga workflow suggest` / `apply`). Your SOPs accrete from what you actually do. See the [Workflows guide](docs/WORKFLOWS_GUIDE.md).

## Capabilities (what's built today)

- **Agents / teams / workflows / tasks as code** — plain YAML; `jigga plan` / `apply` / `validate` show and gate config changes (agents-as-code, not a reconcile engine).
- **Workflows & inference** — declarative, schedulable playbooks (`jigga workflow plan` / `run`); steps chain by named output, can be model-backed, and gate risky steps for approval. **JIGGA also infers and proposes new workflows from your repeated work** (`jigga workflow suggest` / `apply`). See the [Workflows guide](docs/WORKFLOWS_GUIDE.md).
- **Always-on supervisor daemon** — cron/event/channel-driven; wakes temporary agents; loop-prevention (wake throttle + cron dedup).
- **Default agent + first-run setup** — `jigga setup` scaffolds a **chief-of-staff or personal-assistant** default agent (catch-all for inbound, oversees/dispatches to teams) and generates your `USER.md`.
- **Scoped, file-first memory** — raw/team/role layers, **keyword search (sqlite FTS5)**, compaction, an opt-in write-approval queue, and a **per-agent context pack** so agents wake grounded in who they are + what they've done.
- **Teams & shared workspaces** — lead-curated plan/priorities (curator model), **recipe scaffolding** (`jigga team scaffold`), and **file-first handoffs** with an auditable decision log.
- **Capabilities** — filesystem, desktop notifications, Google Calendar/Workspace, memory search/remember, model-backed drafting, controlled **subagent delegation** (incl. **local `codex` / `claude` CLI backends**, run directly instead of via API), **MCP servers**, and cross-team read + dispatch (`team.list` / `team.status` / `team.run` / `task.assign`).
- **Channels** — a normalized **Telegram** gateway (supervisor-polled, activation modes, `jigga channels setup`) with an **approval queue routed back to the channel** (`approve <code>`).
- **Model routing** — dry-run, any OpenAI-compatible endpoint, and **ChatGPT-subscription (OAuth, no API key)**, with provider fallback.
- **Cost & safety** — per-call cost + **per-agent budgets** (hard-stop + warn), **permission modes**, policy gating, a path-canonicalized filesystem gate, and human-in-the-loop approvals.
- **Observability** — JSONL audit log, `jigga logs` / `jigga audit` / `jigga trace`, secret redaction, and log rotation.

## Core Philosophy

**Agents as infrastructure.** You declare agents, teams, workflows, and policies as code — versioned, diffable, reproducible, scaffolded from recipes, and rolled out with `jigga plan` / `apply`. You manage your AI workforce the way you manage infrastructure, not by clicking around a UI.

**File-first.** Everything lives as plain files on disk under `~/.jigga/` — config, memory, tasks, logs, and coordination (handoffs, decisions). No hidden database, no ephemeral message bus. If it happened, there's a file for it: readable, greppable, version-controllable, and auditable.

**You own your data, and it's portable.** Your agents, memory, history, and config are your files — copy, back up, or version the `~/.jigga/` directory and your entire AI workforce moves with it to another machine. JIGGA never *requires* your personal details to live on a third-party server; you bring your own model and credentials, which stay local.

**Agents don't need to run forever.** The **supervisor daemon** is always on. It watches schedules, events, task queues, and agent requests, then wakes agents when there is work to do. Agents run, act, update memory/state, and stop — the feeling of always-on AI workers without wasting resources or creating runaway loops.

## Tool Capability Specs

Implementation-facing tool/capability specs live in [`docs/tools/README.md`](docs/tools/README.md). These include capability packs, elastic delegation/subagents, channel adapters, sessions, scheduler/watchers, safe shell, filesystem tooling, browser automation, notifications, email/calendar connectors, workflow inference, model routing, skill security scanning, and observability.

## Core Components

```text
Events / Cron / User Requests / Agent Requests
        ↓
Supervisor Daemon
        ↓
Agent Runtime + Team Runtime
        ↓
Workflow Library
        ↓
Task Queue
        ↓
Memory Kernel
        ↓
Local Filesystem + Indexes
```

## Design Principles

1. **Agents as infrastructure** — Agents, teams, workflows, and policies are declared as code: versioned, diffable, and rolled out with `jigga plan` / `apply`.
2. **File-first** — All state lives as plain files under `~/.jigga/` — auditable and greppable, with no hidden store or message bus.
3. **You own your data, fully portable** — Your agents, memory, and history are your files; move `~/.jigga/` and everything comes with it. No personal details required on a third-party server.
4. **Local-first** — Memory, state, logs, and configuration live on the user's machine by default.
5. **Declarative** — Users define desired agents, teams, workflows, and policies in files.
6. **Memory-centric** — Agents are temporary executors; memory is the persistent intelligence layer.
7. **Scoped context** — Not every agent sees everything. Memory is filtered by role, need, and trust.
8. **Safe autonomy** — Agents may act independently, but only within explicit permissions.
9. **Workflow-aware** — Repeated work becomes reusable playbooks that can be invoked, proposed, reviewed, and approved.
10. **Agent-to-agent activation** — Agents can delegate tasks and wake other agents through the supervisor.

## Example Agent

```yaml
id: daily_briefing_agent
name: Daily Briefing Agent
role: Summarizes the user's day each morning.
model: gpt-5.5
memory_scope: manager_view
wake:
  schedules:
    - cron: "30 7 * * 1-5"
      event: morning_briefing
permissions:
  calendar: read
  email: read
  notifications: send
  filesystem:
    allow:
      - ~/.jigga/memory/summaries
    deny:
      - ~/.ssh
      - ~/Library/Keychains
workflows:
  - morning_day_summary
```

## Example Workflow

```yaml
id: morning_day_summary
name: Morning Day Summary
purpose: Check calendar and email each weekday morning and summarize the user's day.
trigger:
  schedule: "weekday 7:30am"
steps:
  - id: read_calendar
    action: calendar.list_events
    input:
      range: today
  - id: read_email
    action: email.search
    input:
      filters: [important, unread, today]
  - id: summarize
    agent: daily_briefing_agent
    action: summarize_day
  - id: notify
    action: notifications.send
    approval: not_required
```


## Claude Code-Inspired Core Architecture Guides

These guides adapt useful Claude Code-style primitives into JIGGA's broader AI OS model:

- [Claude Code Patterns to Adopt](docs/core/CLAUDE_CODE_PATTERNS_TO_ADOPT.md)
- [Project Directory & Local AI Layer](docs/core/PROJECT_DIRECTORY.md)
- [Memory & Instructions Files](docs/core/MEMORY_AND_INSTRUCTIONS.md)
- [Permission Modes](docs/core/PERMISSION_MODES.md)
- [Hooks & Lifecycle Events](docs/core/HOOKS_LIFECYCLE.md)
- [Path-Scoped Rules](docs/core/PATH_SCOPED_RULES.md)
- [Context Inspection & Compaction](docs/core/CONTEXT_COMPACTION.md)
- [Subagent Context Isolation](docs/agents/SUBAGENT_CONTEXT_ISOLATION.md)
- [MCP & Capability Registry](docs/tools/MCP_AND_CAPABILITY_REGISTRY.md)

See [`examples/project/`](examples/project/) for an example `.jigga/` project-local AI layer.

## Repository Structure

```text
docs/
  PRODUCT_PLAN.md
  ARCHITECTURE.md
  MEMORY_MODEL.md
  WORKFLOWS.md
  SECURITY_SANDBOXING.md
  MVP_ROADMAP.md
  core/
  agents/
  tools/
schemas/
  agent.schema.yaml
  team.schema.yaml
  workflow.schema.yaml
  task.schema.yaml
  memory-scope.schema.yaml
examples/
  agents/
  teams/
  workflows/
  memory/
```

## Installation (fresh machine)

**Prerequisites:** Python **3.11+** and git. macOS and Linux are supported; Windows is experimental. The only runtime dependency is PyYAML (installed for you).

### Quick start (one command)

```bash
git clone https://github.com/JIGGAI/JIGGA.git
cd JIGGA
./scripts/install.sh               # finds Python 3.11+, builds .venv, upgrades pip, installs
source .venv/bin/activate
```

`scripts/install.sh` runs on a fresh machine *before* `jigga` exists. It probes for a Python 3.11+ (newest first, so it won't settle for macOS's stock 3.9), creates `.venv` with it, upgrades pip past the editable-install cutoff, and installs JIGGA — turning the two most common fresh-machine failures into a clear message instead. Pass `PYTHON=/path/to/python3.12` to force an interpreter, or `make install` if you prefer.

### Manual steps (if you'd rather not run the script)

> **macOS note:** stock macOS ships Python 3.9 as `python3`, which is too old. Install a newer one first (`brew install python@3.12`) and create the venv with it explicitly (`python3.12 -m venv .venv`) — `python3 -m venv` would otherwise reuse 3.9 and the install fails with *"requires a different Python: 3.9.x not in '>=3.11'"*.

```bash
# 1. Get the code
git clone https://github.com/JIGGAI/JIGGA.git
cd JIGGA

# 2. Isolated environment + install (this provides the `jigga` command)
#    Use an explicit 3.11+ interpreter — NOT bare `python3` on macOS (that's 3.9).
python3.12 -m venv .venv           # or python3.11; Linux: python3 is usually fine
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python --version                   # sanity check: must be 3.11+
pip install --upgrade pip          # need pip >= 21.3 for editable installs (PEP 660)
pip install -e .

# 3. Create the local runtime at ~/.jigga (add --examples for sample agents/teams)
jigga init --examples

# 4. First-run setup — who the assistant works for, its purpose, chief-of-staff
#    vs personal-assistant, comms style, and which folders it may access.
#    Generates ~/.jigga/USER.md and your default agent. (Nothing is hardcoded.)
jigga setup

# 5. Connect a model so agents can actually think:
jigga model setup                  # choose a provider
jigga model login                  # ChatGPT subscription via OAuth (no API key)
#    — or configure an OpenAI-compatible endpoint/key in model setup.
#    Skip step 5 to stay on the built-in dry-run provider for a no-cost trial.

# 6. (optional) Connect a chat channel to talk to it:
jigga channels setup               # e.g. Telegram

# 7. Run it:
jigga service install              # keep the supervisor always-on across reboots
#                                    (launchd on macOS / systemd --user on Linux)
#    …or run it in the foreground:
jigga supervisor start             # wakes agents on schedules/events/messages
#    …or one-off:
jigga run agent <agent_id>         # run a single agent now
jigga team run <team_id>           # kick off a team
```

Run against an isolated runtime directory instead of `~/.jigga` with `--home <path>` or `JIGGA_HOME=<path>` — handy for trying recipes without touching your main install.

## CLI reference (most-used)

```bash
jigga init [--examples]            # create the local runtime
jigga setup                        # first-run onboarding (default agent + USER.md)
jigga state                        # inspect agents / teams / workflows / tasks
jigga plan | apply | validate      # show, gate, and apply config changes (agents-as-code)
jigga team scaffold <recipe>       # create a team + agents + workspace from a recipe
jigga team run|handoff|decisions   # run a team / fire a handoff / read the decision log
jigga workflow plan|run <id>       # plan or run a workflow
jigga run agent <id>               # run one agent
jigga supervisor start|tick        # the always-on daemon (or a single tick)
jigga service install|status|uninstall  # run the supervisor as a launchd/systemd user service
jigga model setup|login|status     # configure / authenticate the model provider
jigga channels setup|status        # connect a chat channel (Telegram)
jigga memory search <query>        # search scoped memory
jigga cost [--since 7d]            # per-agent model spend + budgets
jigga trace <id> | audit | logs    # observability
jigga approvals list|approve <code># human-in-the-loop
```

## Status

A working local-first runtime: **545 passing tests** across the supervisor, agent/team runtimes, scoped memory, channels, model routing, cost/budgets, observability, and the default-agent + setup flow. Standard library + PyYAML only.

Roadmap to v1.0 (see [`docs/ROADMAP_TO_PRODUCTION.md`](docs/ROADMAP_TO_PRODUCTION.md)): finish real connectors (email), OS-level isolation (sandbox + secrets broker), and distribution (`pip install jigga`, service autostart, a dashboard).

See [`docs/PRODUCT_PLAN.md`](docs/PRODUCT_PLAN.md), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), and [`docs/AGENT_TEAM_GUIDE.md`](docs/AGENT_TEAM_GUIDE.md) for the deeper design + how to build agent teams.

## Additional Architecture Guides

- [Elastic Delegation & Subagent Spawning](docs/tools/ELASTIC_DELEGATION_SUBAGENTS.md) — defines how primary agents can spawn bounded subagents through Codex, Claude Code, or future runtime backends.

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

## Core Philosophy

Agents do not need to run forever.

The **supervisor daemon** is always on. It watches schedules, events, task queues, and agent requests, then wakes agents when there is work to do. Agents run, act, update memory/state, and stop. This creates the feeling of always-on AI workers without wasting resources or creating runaway loops.

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

1. **Local-first** — Memory, state, logs, and configuration live on the user's machine by default.
2. **Declarative** — Users define desired agents, teams, workflows, and policies in files.
3. **Memory-centric** — Agents are temporary executors; memory is the persistent intelligence layer.
4. **Scoped context** — Not every agent sees everything. Memory is filtered by role, need, and trust.
5. **Safe autonomy** — Agents may act independently, but only within explicit permissions.
6. **Workflow-aware** — Repeated work becomes reusable playbooks that can be invoked, proposed, reviewed, and approved.
7. **Agent-to-agent activation** — Agents can delegate tasks and wake other agents through the supervisor.

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

## Repository Structure

```text
docs/
  PRODUCT_PLAN.md
  ARCHITECTURE.md
  MEMORY_MODEL.md
  WORKFLOWS.md
  SECURITY_SANDBOXING.md
  MVP_ROADMAP.md
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

## Terraform-Style CLI Direction

Planned interface:

```bash
jigga init
jigga plan
jigga apply
jigga state
jigga run agent daily_briefing_agent
jigga workflow plan morning_day_summary
jigga workflow apply morning_day_summary
```

## Status

This repo currently contains the initial product and architecture plan, starter schemas, and example configurations.

See [`docs/PRODUCT_PLAN.md`](docs/PRODUCT_PLAN.md) and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full handoff plan.

## Additional Architecture Guides

- [Elastic Delegation & Subagent Spawning](docs/tools/ELASTIC_DELEGATION_SUBAGENTS.md) — defines how primary agents can spawn bounded subagents through Codex, Claude Code, or future runtime backends.

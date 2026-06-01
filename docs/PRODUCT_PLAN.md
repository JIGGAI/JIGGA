# JIGGA Product Plan

## One-Line Definition

JIGGA is a local-first, Terraform-style operating system for personal AI workers, reusable agent teams, shared scoped memory, and declarative workflows.

## Product Thesis

Most agent frameworks are either developer SDKs, chatbot wrappers, or workflow chains that only run when explicitly invoked. JIGGA treats agents like personal AI workers: they have roles, tasks, memory scopes, permissions, and reusable operating procedures.

The system feels always-on because the supervisor, task queues, memory, schedules, and state persist. Individual agents are woken only when they have work to do.

## Target User

Initial target users:

- Builders who want persistent personal AI workers.
- Founders and operators who want repeatable AI workflows.
- Developers who want local-first agent orchestration.
- Power users who want AI teams for content, research, software, admin, and operations.

## Core Nouns

### Agent

An individual AI worker with a role, model, tools, permissions, memory scope, and wake conditions.

### Team

A reusable group of agents configured to work together on a category of work.

### Workflow

A declarative playbook or standard operating procedure that agents can invoke for repeatable work.

Workflows are not a central mandatory engine. They are reusable operating procedures available to agents, teams, and users.

### Task

A unit of work that can be created, claimed, delegated, executed, reviewed, and completed.

### Memory

Persistent local knowledge, stored file-first, indexed for retrieval, and exposed to agents through scoped summaries or filtered context.

### Policy

Permission and safety constraints for what agents can see, do, execute, and persist.

### Supervisor

The always-on daemon that wakes agents based on cron, events, user requests, task queue updates, file changes, or agent-to-agent delegation.

## Core Product Capabilities

### 1. Declarative Agent Configuration

Users define agents in configuration files instead of hardcoding behavior.

Agents include:

- id
- name
- role
- model
- tools
- memory scope
- permissions
- wake conditions
- allowed workflows

### 2. Always-On Supervisor

The supervisor daemon runs continuously and handles:

- cron schedules
- external events
- task queue updates
- agent wake requests
- file watchers
- manual user requests

The supervisor does not perform all work itself. It activates the correct agent or team runtime.

### 3. Agent-to-Agent Activation

Agents can delegate work to other agents through the supervisor.

Example:

1. Research agent finds a content opportunity.
2. It creates a task for the strategist.
3. The supervisor wakes the strategist.
4. The strategist invokes a social syndication workflow.

### 4. Team Recipes

Users can stand up teams from declarative files.

Examples:

- software_delivery_team
- social_content_team
- research_team
- personal_admin_team
- sales_outreach_team

Teams define which agents collaborate, default workflows, shared memory scope, policies, and operating rules.

### 5. Workflow Library

Workflows are easily declared action sequences for repeatable tasks.

Examples:

- morning_day_summary
- meeting_reminders
- social_content_syndication
- ship_feature
- research_brief
- inbox_triage

Agents can invoke workflows when they recognize a task matches a known procedure.

### 6. Workflow Inference

JIGGA should detect repeated patterns and suggest reusable workflows.

Example pattern:

- User asks every morning: “Check my calendar and email and summarize my day.”
- Agent notices repetition.
- Agent proposes a workflow.
- User reviews the plan.
- User approves applying it.

Agents should not silently create recurring autonomous workflows without user approval.

### 7. Local-First Shared Memory

Memory lives locally by default.

Memory is stored as:

- raw files
- logs
- transcripts
- structured facts
- summaries
- indexes

Cloud sync/backup can be optional, encrypted, and user-controlled later.

### 8. Scoped Memory Access

Not every agent should know everything.

Memory scopes allow agents to receive different levels of context:

- full_user
- manager_view
- project_view
- task_only
- minimal

This mirrors human organizations: a close collaborator knows more than an occasional contractor.

### 9. Safe Autonomy

Agents may act independently, but only within policy.

Policies govern:

- memory access
- filesystem access
- shell access
- network access
- tool use
- approvals
- schedule creation
- workflow activation

### 10. Agents-as-Code & Config Diff

"Terraform-style" here means **agents and teams are code** — declarative, version-controllable definitions authored directly or generated from recipes (the ClawRecipes model), not a literal Terraform reconcile engine. The companion safety layer is a config diff: before enabling a new agent, team, workflow, or permission change, JIGGA shows a plan (`jigga plan`) of what will change and gates permission-affecting changes for explicit approval before `jigga apply`.

Example:

```text
This workflow will:
- read your calendar every weekday at 7:30am
- search important unread emails
- generate a day summary
- send a notification

Requires permissions:
- calendar: read
- email: read
- notifications: send
- schedule: weekday mornings
```

## Differentiation

### Compared to LangChain

LangChain is a developer framework. JIGGA is an infrastructure/runtime layer for persistent AI workers.

### Compared to OpenClaw-style assistants

JIGGA adopts the always-on supervisor concept but adds declarative teams, scoped memory, repeatable workflows, and Terraform-style planning.

### Compared to Hermes-style memory agents

JIGGA adopts persistent personal memory and learning patterns but makes memory scoping, local files, permissions, and approval explicit.

### Compared to PraisonAI-style orchestration

JIGGA uses multi-agent teams and workflows but is more local-first, file-first, stateful, and infrastructure-oriented.

## Product Positioning

JIGGA is not a chatbot.
JIGGA is not only an automation builder.
JIGGA is not only an agent framework.

JIGGA is an operating system for personal AI workers.

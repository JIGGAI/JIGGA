# MVP Roadmap

## Goal

Build a minimal but real JIGGA runtime that proves the core concept:

- always-on supervisor
- temporary agent runtimes
- local file-first memory
- scoped memory
- task queue
- declarative workflows
- agent-to-agent wake/delegation
- workflow suggestion from repeated patterns

## V1 Constraints

Do not build everything.

Avoid:

- full GUI
- advanced multi-agent negotiation
- perfect memory
- microVM sandboxing
- cloud sync
- fully autonomous workflow activation
- enterprise permissions

## 2–4 Week MVP

### Week 1: Runtime Foundation

Deliverables:

- repo structure
- supervisor daemon skeleton
- agent config loader
- basic agent runner
- local state file
- simple task queue
- logging

Commands:

```bash
jigga init
jigga supervisor start
jigga run agent <agent_id>
jigga state
```

### Week 2: Memory + Workflows

Deliverables:

- local memory directory
- raw memory writes
- structured memory files
- summary memory files
- memory scopes
- workflow YAML loader
- basic workflow executor

Commands:

```bash
jigga memory inspect
jigga workflow run <workflow_id>
jigga workflow plan <workflow_id>
```

### Week 3: Delegation + Cron

Deliverables:

- cron trigger support
- agent wake requests
- agent-to-agent task delegation
- task state transitions
- team runtime skeleton
- example daily briefing workflow
- example social content team

Commands:

```bash
jigga task list
jigga task create
jigga team run <team_id>
```

### Week 4: Safety + Workflow Inference

Deliverables:

- permission model v1
- filesystem allow/deny
- restricted shell mode
- approval gates
- audit logs
- basic repeated-pattern detector
- workflow suggestion output

Commands:

```bash
jigga plan
jigga apply
jigga workflow suggest
jigga workflow apply <workflow_id>
```

## MVP Demo Scenarios

### Demo 1: Morning Day Summary

User approves a recurring workflow that:

- wakes every weekday morning
- reads calendar
- scans important unread email
- summarizes the day
- sends notification

### Demo 2: Meeting Reminders

Workflow that:

- monitors upcoming calendar events
- notifies user 30 minutes before
- notifies user 5 minutes before
- optionally includes prep notes

### Demo 3: Social Content Syndication

Team that:

- takes source material
- extracts core message
- drafts LinkedIn post
- drafts X thread
- drafts newsletter blurb
- sends to editor
- prepares publishing package

### Demo 4: Agent Delegation

Research agent discovers a content opportunity and wakes the content strategist agent by creating a task.

### Demo 5: Workflow Inference

System notices repeated morning briefing requests and suggests a workflow.

## Success Criteria

MVP succeeds if:

- users can define agents, teams, and workflows in files
- supervisor can wake agents from schedules/events
- agents can create tasks for other agents
- memory persists locally
- agents receive scoped memory
- workflows are reusable
- inferred workflows require approval
- permissions are visible and enforced at a basic level

## Post-MVP Features

- GUI/dashboard
- richer workflow planner
- vector index optimization
- encrypted cloud sync
- plugin marketplace
- stronger sandboxing
- microVM support
- workflow marketplace
- team templates
- advanced observability
- memory visualization

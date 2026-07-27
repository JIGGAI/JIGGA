# JIGGA Architecture

## Architectural Summary

JIGGA uses an always-on supervisor daemon to wake temporary agent runtimes. Agents execute tasks, invoke workflows, update memory/state, and terminate. The persistence layer is not the agent process. It is the combination of memory, task queues, workflow definitions, schedules, and state.

## High-Level Architecture

```text
┌──────────────────────────────────────────────┐
│ Inputs                                       │
│ - Cron schedules                             │
│ - User requests                              │
│ - File changes                               │
│ - External events                            │
│ - Agent wake requests                        │
│ - Task queue changes                         │
└─────────────────────┬────────────────────────┘
                      ↓
┌──────────────────────────────────────────────┐
│ Supervisor Daemon                            │
│ - Event polling                              │
│ - Schedule handling                          │
│ - Target resolution                          │
│ - Wake decisions                             │
│ - Loop prevention                            │
└─────────────────────┬────────────────────────┘
                      ↓
┌──────────────────────────────────────────────┐
│ Runtime Layer                                │
│ - Agent Runtime                              │
│ - Team Runtime                               │
│ - Tool Runtime                               │
└─────────────────────┬────────────────────────┘
                      ↓
┌──────────────────────────────────────────────┐
│ Work Layer                                   │
│ - Task Queue                                 │
│ - Workflow Library                           │
│ - Delegation                                 │
│ - Approvals                                  │
└─────────────────────┬────────────────────────┘
                      ↓
┌──────────────────────────────────────────────┐
│ Memory Kernel                                │
│ - Raw memory                                 │
│ - Structured memory                          │
│ - Summaries                                  │
│ - Indexes                                    │
│ - Scoped retrieval                           │
└─────────────────────┬────────────────────────┘
                      ↓
┌──────────────────────────────────────────────┐
│ Local Storage                                │
│ ~/.jigga                                     │
│ - agents                                     │
│ - teams                                      │
│ - workflows                                  │
│ - memory                                     │
│ - tasks                                      │
│ - state                                      │
│ - logs                                       │
└──────────────────────────────────────────────┘
```

## Supervisor Daemon

The supervisor is the only component that must be always on.

Responsibilities:

- Poll schedules.
- Listen for events.
- Watch selected files/directories.
- Inspect task queues.
- Accept agent wake requests.
- Resolve which agent/team should run.
- Enforce basic rate limits and loop prevention.
- Start agent runtimes.

Pseudo-code:

```python
while True:
    events = collect_events()

    for event in events:
        targets = resolve_targets(event)

        for target in targets:
            if allowed_to_wake(target, event):
                run_target(target, event)

    sleep(SUPERVISOR_INTERVAL)
```

## Agent Runtime

An agent runtime is temporary. It starts for a task/event, executes, writes outputs, and stops.

Agent runtime flow:

```python
def run_agent(agent_id, event):
    config = load_agent_config(agent_id)
    policy = load_policy(config)
    context = memory.load_scope(config.memory_scope, event)
    inbox = task_queue.get_for_agent(agent_id)

    decision = model.decide(
        role=config.role,
        event=event,
        inbox=inbox,
        context=context,
        tools=config.tools,
        policy=policy,
    )

    results = execute_decision(decision, policy)
    memory.write(results.memory_updates)
    task_queue.write(results.task_updates)
    state.write(results.state_updates)
```

## Team Runtime

A team is a configured group of agents. The team runtime is responsible for routing tasks among team members.

Responsibilities:

- Load team configuration.
- Determine initial agent assignment.
- Coordinate handoffs.
- Maintain team-specific task state.
- Apply team-level policies and memory scopes.
- Invoke relevant workflows.

Teams are not necessarily always running. They are activated by the supervisor or by agent delegation.

## Workflow Library

Workflows are reusable playbooks, not a mandatory central orchestration engine.

A workflow can be invoked by:

- user request
- agent decision
- team runtime
- schedule
- external event

Workflow responsibilities:

- Define repeatable steps.
- Declare expected inputs and outputs.
- Define which agents/tools are used.
- Define approvals/gates.
- Define failure handling.
- Define memory writes.

## Task Queue

The task queue is the coordination layer for work.

Tasks can be:

- created by users
- created by agents
- created by workflows
- created by schedules
- delegated to other agents
- assigned to teams

Task states:

- pending
- claimed
- running
- blocked
- needs_approval
- failed
- completed
- archived

## Memory Kernel

The memory kernel exposes scoped context to agents.

It should support:

- raw local files
- structured facts
- summaries
- semantic search
- keyword search
- memory compaction
- role-based retrieval
- trust-based filtering

## Local Directory Structure

Recommended runtime directory:

```text
~/.jigga/
  config.yaml
  state.json
  agents/
  teams/
  workflows/
  tasks/
  memory/
    raw/
    structured/
    summaries/
    indexes/
  logs/
  policies/
```

## Event Model

Events should have a common shape:

```yaml
id: event_abc123
type: cron.tick
source: supervisor
created_at: 2026-04-23T07:30:00-04:00
targets:
  - daily_briefing_agent
payload:
  schedule: weekday_morning
```

## Wake Sources

Agents can be woken by:

1. Cron schedule
2. External event
3. User request
4. File change
5. Task queue update
6. Another agent
7. Workflow step

## Loop Prevention

The system must prevent runaway behavior.

Initial controls:

- max wake count per agent per interval
- max delegation depth
- max workflow recursion depth
- cooldown windows
- approval for new recurring workflows
- task deduplication
- repeated failure backoff

## State Management

State should be local, diffable, and inspectable.

Example:

```json
{
  "agents": {
    "daily_briefing_agent": {
      "status": "enabled",
      "last_run": "2026-04-23T07:30:00-04:00"
    }
  },
  "workflows": {
    "morning_day_summary": {
      "status": "approved",
      "version": "1.0.0"
    }
  }
}
```

## CLI Direction

JIGGA should feel familiar to infrastructure users.

```bash
jigga init
jigga plan
jigga apply
jigga state
jigga run agent <agent_id>
jigga team run <team_id>
jigga workflow plan <workflow_id>
jigga workflow run <workflow_id>
jigga supervisor start
jigga service status
```

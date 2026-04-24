# Scheduler, Heartbeat & Event Watchers

## Purpose

JIGGA should feel like actual workers are present without keeping every agent process running forever.

The Supervisor stays alive. It wakes agents based on schedules, events, task queue changes, and agent requests.

## Product Definition

The **Scheduler** handles time-based triggers. The **Heartbeat** periodically checks system state. **Event Watchers** listen for external or local changes.

## Trigger Types

```yaml
triggers:
  - cron: "0 7 * * MON-FRI"
  - interval: "15m"
  - file_changed: ./content/inbox/**
  - task_created: true
  - agent_requested: true
  - webhook: /events/github
```

## Architecture

```text
Scheduler + Watchers
  ↓
Event Queue
  ↓
Supervisor
  ↓
Target Resolver
  ↓
Agent / Team / Workflow
```

## Example: Morning Briefing

```yaml
schedule:
  name: weekday_morning_briefing
  cron: "30 7 * * MON-FRI"
  target:
    agent: daily_briefing_agent
  payload:
    action: check_calendar_and_email
```

## Example: Meeting Reminders

```yaml
watcher:
  name: meeting_reminders
  source: calendar
  offsets:
    - 30m
    - 5m
  target:
    agent: personal_admin
```

## Anti-Loop Rules

- Deduplicate identical events in a time window.
- Add max runs per agent per hour.
- Add cooldowns.
- Prevent recursive wake storms.
- Record event lineage.

## APIs

```ts
interface Scheduler {
  register(trigger: Trigger): Promise<void>
  unregister(id: string): Promise<void>
  due(now: Date): Promise<JiggaEvent[]>
}
```

```ts
interface EventWatcher {
  name: string
  start(emit: (event: JiggaEvent) => void): Promise<void>
  stop(): Promise<void>
}
```

## V1 Build Tasks

- Implement cron triggers.
- Implement task-queue triggers.
- Implement file watcher.
- Add event deduplication.
- Add cooldown policy.

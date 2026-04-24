# Session Manager

## Purpose

JIGGA needs persistent sessions for agents, subagents, channel conversations, long-running tasks, and background tool executions.

A session is not the same as memory. A session is the runtime record of a bounded interaction or job.

## Product Definition

A **Session** tracks execution state, messages, tool calls, outputs, logs, and lifecycle status for an agent or tool runtime.

## Session Types

- `agent`: normal agent run
- `team`: coordinated team run
- `workflow`: workflow/playbook execution
- `subagent`: delegated Codex/Claude Code style worker
- `channel`: conversation with user through an adapter
- `tool`: long-running tool process

## Session Schema

```yaml
session:
  id: sess_123
  type: subagent
  parent_session: sess_parent
  agent: engineer
  status: running
  started_at: 2026-04-23T09:00:00-04:00
  workspace: ./apps/api
  memory_scope: project_view_minimal
  permissions: restricted
```

## Lifecycle

```text
created → running → waiting → completed
                 ↘ failed
                 ↘ cancelled
                 ↘ timed_out
```

## APIs

```ts
interface SessionManager {
  create(input: CreateSessionInput): Promise<Session>
  send(sessionId: string, message: SessionMessage): Promise<void>
  history(sessionId: string): Promise<SessionHistory>
  list(filter?: SessionFilter): Promise<Session[]>
  cancel(sessionId: string): Promise<void>
  summarize(sessionId: string): Promise<SessionSummary>
}
```

## CLI

```bash
jigga sessions list
jigga sessions history sess_123
jigga sessions cancel sess_123
jigga sessions summarize sess_123
```

## Memory Boundary

Sessions write raw logs to session storage. Only approved summaries/facts should be promoted into shared memory.

```text
session logs → summarizer → memory write proposal → memory kernel
```

## V1 Build Tasks

- Add session storage in `~/.jigga/sessions`.
- Track agent runs and workflow runs.
- Support parent-child sessions.
- Add session history CLI.
- Add timeout/cancellation support.

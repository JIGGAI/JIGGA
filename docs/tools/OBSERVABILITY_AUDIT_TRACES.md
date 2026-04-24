# Observability, Audit Logs & Traces

## Purpose

Always-on agents need visibility. Users and developers must understand what agents did, why they did it, what tools they used, and what memory they accessed or changed.

## Product Definition

JIGGA should maintain structured audit logs and traces for every agent run, workflow, tool call, memory write, notification, and permission decision.

## Trace Shape

```yaml
trace:
  id: trace_123
  session: sess_456
  agent: personal_admin
  event: calendar_upcoming
  decisions:
    - loaded_memory_scope: manager_view
    - invoked_tool: calendar_read_event
    - invoked_tool: notify_user
  outputs:
    - notification_sent
```

## Audit Log Events

- Agent started
- Agent completed
- Tool invoked
- Tool denied
- Permission requested
- Memory read
- Memory write proposed
- Memory write approved
- Workflow suggested
- Workflow enabled
- Notification sent
- External message sent

## Storage

```text
~/.jigga/logs/
  audit.jsonl
  traces/
    trace_123.json
```

## CLI

```bash
jigga logs tail
jigga trace trace_123
jigga audit --agent personal_admin --since 24h
```

## Redaction

Logs should redact:

- API keys
- OAuth tokens
- passwords
- private keys
- session cookies
- sensitive file contents

## V1 Build Tasks

- Add JSONL audit logger.
- Add trace IDs to every session.
- Log tool calls and permission checks.
- Add basic CLI inspection.
- Add secret redaction middleware.

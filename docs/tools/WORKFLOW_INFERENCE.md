# Workflow Inference

## Purpose

JIGGA should learn repeated user patterns and suggest reusable workflows. This makes the system feel like it improves over time without requiring the user to write every workflow manually.

## Product Definition

**Workflow Inference** detects repeated actions, prompts, schedules, and tool chains, then proposes a workflow draft for user approval.

## Examples

- User asks for a morning email/calendar summary several times.
- User repeatedly asks to turn blog posts into LinkedIn and X drafts.
- User asks to review pull requests using the same checklist.
- Agent repeatedly performs meeting prep before calendar events.

## Detection Signals

```yaml
signals:
  - repeated_prompt_similarity
  - repeated_tool_sequence
  - recurring_time_pattern
  - repeated_file_paths
  - repeated_output_format
  - repeated_agent_chain
```

## Suggested Workflow Output

```yaml
suggested_workflow:
  name: morning_day_summary
  confidence: 0.86
  based_on:
    - "Calendar + email summary requested 5 times in 14 days"
    - "Requests occurred between 7:00 AM and 8:00 AM"
  trigger:
    schedule: "weekdays at 7:30 AM"
  actions:
    - check_calendar
    - check_email
    - summarize_day
    - notify_user
```

## Approval Flow

```text
Detect pattern
  ↓
Draft workflow
  ↓
Show plan/diff
  ↓
User approves
  ↓
Workflow enabled
```

## CLI

```bash
jigga workflow suggestions
jigga workflow plan morning_day_summary
jigga workflow apply morning_day_summary
```

## Safety Rules

- Never auto-enable recurring workflows without approval.
- Require explicit permissions for email, calendar, shell, browser, or outbound messaging.
- Store inference evidence so the user can understand why it was suggested.
- Allow users to reject and suppress a suggestion.

## V1 Build Tasks

- Log normalized action chains.
- Add similarity grouping for repeated prompts.
- Add simple heuristic workflow suggester.
- Add suggestion review CLI.

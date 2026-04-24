# Email & Calendar Connectors

## Purpose

Personal workers need to understand the user's day. Email and calendar access enable daily briefings, meeting prep, reminders, scheduling support, and follow-up tracking.

## Product Definition

The **Email Connector** and **Calendar Connector** expose scoped, read-first tools for personal admin agents.

## Calendar Tools

```yaml
tools:
  - calendar_list_events
  - calendar_read_event
  - calendar_create_event
  - calendar_update_event
  - calendar_rsvp
```

## Email Tools

```yaml
tools:
  - email_search
  - email_read
  - email_draft
  - email_send
  - email_label
  - email_archive
```

## Read-First Defaults

```yaml
connectors:
  email:
    default_mode: read_only
    send_requires_approval: true
  calendar:
    default_mode: read_only
    write_requires_approval: true
```

## Morning Briefing Example

```yaml
workflow: morning_day_summary
steps:
  - calendar_list_events:
      range: today
  - email_search:
      query: "is:unread newer_than:24h"
  - summarize_day:
      include:
        - meetings
        - urgent emails
        - prep items
        - follow-ups
  - notify_user
```

## Meeting Reminder Example

```yaml
trigger:
  calendar_event_upcoming:
    offsets: [30m, 5m]
actions:
  - prepare_meeting_context
  - notify_user
```

## Safety Rules

- Draft before send by default.
- Never email third parties without approval.
- Never delete email by default; archive/label only.
- Store only summaries in shared memory unless explicitly approved.
- Meeting attendee lists may be sensitive; scope carefully.

## V1 Build Tasks

- Add abstract connector interface.
- Implement calendar read/list.
- Implement email search/read.
- Implement draft creation, not send.
- Add meeting reminder watcher.

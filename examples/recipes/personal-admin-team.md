---
id: personal_admin_team
name: Personal Admin Team
kind: team
version: 0.2.0
description: Help the user manage daily schedule, reminders, inbox, and recurring personal workflows.
purpose: Help the user manage daily schedule, reminders, inbox, and recurring personal workflows.
memory_scope: manager_view
routing:
  lead: daily summary
default_workflows:
  - morning_day_summary
  - meeting_reminders
policies:
  approvals:
    required_for:
      - send_email
      - create_recurring_workflow
      - delete_event
agents:
  - role: daily summary
    id: daily_briefing_agent
    required: true
    agent:
      name: Daily Briefing Agent
      role: Summarizes the user's calendar, email, and priorities each morning.
      description: A trusted personal admin agent that prepares a concise morning briefing.
      model: gpt-5.5
      memory_scope: manager_view
      tools:
        - calendar.list_events
        - calendar.get_event
        - email.search
        - notifications.send
        - summarize_day
        - memory.write_summary
        - memory.search
      # Where notifications.send reaches the user: "default" = the user's default
      # connected channel (config channels.default, set when they connect one), or
      # a specific channel name, or "desktop" for desktop-notification only.
      notifications:
        channel: default
      wake:
        schedules:
          - cron: "30 7 * * 1-5"
            event: morning_briefing
            message: >
              Prepare the morning briefing. Gather today's calendar events with
              calendar.list_events and recent important email with email.search,
              then compose one concise briefing (schedule, highlights, suggested
              priorities). You MUST deliver it by calling notifications.send with
              title "Morning briefing" and the briefing text as body — the task is
              not done until the notification is sent.
        events:
          - task.assigned.daily_briefing_agent
        accepts_agent_requests: true
      permissions:
        memory:
          scope: manager_view
        calendar: read
        email: read
        notifications: send
        filesystem:
          allow:
            - ~/.jigga/memory/summaries
          deny:
            - ~/.ssh
            - ~/.aws
            - ~/Library/Keychains
        network:
          mode: deny
        shell:
          mode: deny
      workflows:
        - morning_day_summary
        - meeting_reminders
  # Membership-only roles: on the roster so routing/workflows can reference
  # them; staff them later (scaffold or hand-write the agent yaml).
  - role: meeting prep
    id: meeting_prep_agent
    required: false
  - role: email triage
    id: inbox_triage_agent
    required: false
workflows:
  - id: morning_day_summary
    name: Morning Day Summary
    purpose: Check calendar and email each weekday morning and summarize the user's day.
    status: approved
    trigger:
      schedule: "weekday 7:30am"
    inputs: {}
    steps:
      - id: read_calendar
        agent: daily_briefing_agent
        action: calendar.list_events
        input:
          range: today
        output: calendar_events
        approval: not_required
      - id: read_email
        agent: daily_briefing_agent
        action: email.search
        input:
          filters:
            - important
            - unread
            - today
        output: important_email
        approval: not_required
      - id: summarize
        agent: daily_briefing_agent
        action: summarize_day
        input:
          calendar: ${calendar_events}
          email: ${important_email}
        output: day_summary.md
        approval: not_required
      - id: notify
        agent: daily_briefing_agent
        action: notifications.send
        input:
          content: ${day_summary.md}
        approval: not_required
    outputs:
      - day_summary.md
    memory:
      write_summary: true
      write_raw: false
    permissions:
      required:
        - calendar.read
        - email.read
        - notifications.send
  - id: meeting_reminders
    name: Meeting Reminders
    purpose: Notify the user before meetings and optionally include prep context.
    status: approved
    trigger:
      event: calendar.event_upcoming
      offsets:
        - 30m
        - 5m
    steps:
      - id: load_event
        agent: daily_briefing_agent
        action: calendar.get_event
        output: event_details
        approval: not_required
      - id: gather_prep_context
        agent: meeting_prep_agent
        action: summarize_relevant_context
        input:
          event: ${event_details}
        output: prep_notes
        optional: true
        approval: not_required
      - id: notify
        agent: daily_briefing_agent
        action: notifications.send
        input:
          include:
            - title
            - time
            - location_or_link
            - attendees
            - prep_notes
        approval: not_required
    outputs:
      - meeting_notification
    memory:
      write_summary: false
      write_raw: false
    permissions:
      required:
        - calendar.read
        - notifications.send
---

# Personal Admin Team

The default personal setup: a briefing agent that wakes weekday mornings,
gathers your calendar + email, and delivers a concise briefing to your
connected channel (Telegram etc.) via `notifications.send` — batteries
included, nothing to grant or edit.

`meeting_prep_agent` and `inbox_triage_agent` are membership-only roles:
referenced by the team and its workflows, staffed when you're ready.

```bash
jigga recipes scaffold personal-admin-team
```

# Notification Router

## Purpose

JIGGA needs to notify the user at the right time, on the right channel, with the right urgency.

## Product Definition

The **Notification Router** sends messages from agents/workflows to user-approved channels such as desktop notifications, CLI, Slack DM, email, or mobile push.

## Notification Schema

```yaml
notification:
  id: notif_123
  recipient: user
  urgency: normal
  title: "Meeting in 30 minutes"
  body: "Product review starts at 10:00 AM. Prep notes are ready."
  channels:
    preferred:
      - desktop
      - slack_dm
```

## Urgency Levels

- `low`: digest only
- `normal`: standard notification
- `high`: interruptive notification
- `critical`: reserved, requires explicit user opt-in

## Routing Policy

```yaml
notifications:
  quiet_hours:
    start: "22:00"
    end: "07:00"
  defaults:
    low: digest
    normal: desktop
    high: desktop_and_mobile
  require_approval_for:
    - external_recipients
```

## Use Cases

- Meeting reminders at 30 minutes and 5 minutes.
- Daily briefing summaries.
- Failed workflow alerts.
- Approval requests.
- Agent task completion updates.

## APIs

```ts
interface NotificationRouter {
  send(notification: Notification): Promise<DeliveryResult>
  schedule(notification: Notification, when: Date): Promise<void>
  digest(userId: string, period: TimeRange): Promise<Digest>
}
```

## V1 Build Tasks

- Implement local desktop notifications.
- Implement CLI notifications.
- Implement scheduled notifications.
- Add quiet hours.
- Add notification audit log.

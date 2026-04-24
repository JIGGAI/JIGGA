# Channel Gateway & Message Adapters

## Purpose

JIGGA should feel always available through the places the user already works: terminal, local UI, Slack, Discord, SMS bridges, email, or other channels.

The Channel Gateway is the boundary between external messages and internal agent/task execution.

## Product Definition

A **Channel Adapter** receives messages or events from an external surface and converts them into JIGGA events.

Examples:

- CLI command
- Local desktop app
- Slack DM
- Discord channel
- Email inbox
- Webhook
- Mobile push action

## Architecture

```text
External Channel
  ↓
Channel Adapter
  ↓
Gateway Event Normalizer
  ↓
Policy / Identity Check
  ↓
Supervisor
  ↓
Agent / Team / Workflow
```

## Normalized Event

```yaml
event:
  id: evt_123
  source: slack
  actor:
    type: user
    id: user_abc
  conversation:
    id: slack_dm_456
  message:
    text: "Give me my day summary"
    attachments: []
  target:
    agent: personal_admin
```

## Adapter Contract

```ts
interface ChannelAdapter {
  name: string
  start(): Promise<void>
  stop(): Promise<void>
  send(reply: ChannelReply): Promise<void>
  normalize(raw: unknown): Promise<JiggaEvent>
}
```

## Routing Modes

```yaml
channels:
  slack:
    activation: mention
    default_agent: personal_admin
  cli:
    activation: always
    default_agent: main
```

Supported activation modes:

- `always`
- `mention`
- `direct_message_only`
- `disabled`

## Safety Rules

- Public/group channels should default to restricted memory.
- Channel messages may contain prompt injection.
- Never expose full personal memory to an untrusted channel.
- Require confirmation before sending messages to third parties.
- Log all outbound messages.

## V1 Build Tasks

- Implement CLI channel.
- Implement local webhook channel.
- Add normalized event schema.
- Add channel-to-agent routing.
- Add outbound reply API.

# Channels: Telegram Runtime Notes

First **Milestone B** channel. A channel lets JIGGA receive messages from and reply to an external surface. Per the project direction, **each channel is a configurable opt-in capability** — users enable the ones they want (`jigga capabilities install telegram`, later `slack`, `imessage`, ...).

## Design: channel = capability with two actions

Rather than a long-lived supervisor daemon, a channel is realised as a capability exposing:

- `telegram.poll_messages` — **inbound**. `getUpdates` (long-poll), allowlist filter, normalize, advance offset.
- `telegram.send_message` — **outbound**. `sendMessage`.

This fits JIGGA's existing model with **zero supervisor changes**: a cron-triggered workflow polls on a cadence (poll → process → reply), exactly like the morning-briefing workflow polls calendar/email. "Always listening" = a scheduled poll workflow. Push-based channels (webhooks) can add a gateway later; polling needs no public endpoint.

All HTTP is in-process `urllib` — native-action category (no subprocess, not sandboxed), same as the Google Calendar connector.

## Normalized channel-message shape

`poll_messages` returns messages in a shape every channel should reuse, so workflows handling Telegram / Slack / iMessage look the same:

```python
{
  "channel": "telegram",
  "chat_id": <int|str>,    # where to reply
  "sender": <str>,         # username / first name
  "sender_id": <int|str>,
  "text": <str>,
  "message_id": <int|str>,
  "date": <int>,           # unix ts
  "update_id": <int>,      # telegram-specific; offset bookkeeping
}
```

## Safety

- **Allowlist by default.** Inbound messages are dropped unless the sender's chat ID is in `channels.telegram.allowed_chat_ids`. With no allowlist, `poll_messages` returns nothing (a bot token is publicly reachable — anyone could DM it). The result includes a `dropped` count and a `note` explaining the default-deny.
- **Discovery escape hatch.** `poll_messages(discover=True)` (CLI `jigga telegram discover`) bypasses the allowlist *for setup only* so you can find your chat ID, and does **not** advance the offset.
- Channel text is **untrusted and prompt-injectable** — the capability is `risk_level: medium`; scope downstream agents accordingly and never expose full memory to a channel-driven agent.
- Bot token stored at `~/.jigga/secrets/telegram_bot_token` (0600). Read offset at `~/.jigga/channels/telegram_state.json`.

## Setup

```bash
jigga capabilities install telegram
#   1. Message @BotFather → /newbot → get a token like 123456789:AAE...
#   2. Wizard stores the token, optionally runs a discovery poll
#   3. You provide allowed chat IDs (discovery can prefill them) + default agent
```

Finding your chat ID: message your bot anything, then either accept the wizard's discovery step or run `jigga telegram discover`.

## CLI

```bash
jigga telegram status     # token present? allowlist? read offset? (JSON)
jigga telegram discover   # poll once, allowlist bypassed, to find your chat ID
jigga telegram logout     # delete the stored bot token
```

## The auto-reply loop (current limitation + follow-up)

This PR delivers inbound (poll → messages) and outbound (send) as separate actions. A full **receive → think → reply** loop where an agent automatically answers each message needs the agent runtime to *invoke capabilities* (call `telegram.send_message` itself), which it doesn't do yet — `run_agent` currently does a model call and writes an artifact, it doesn't dispatch tool/capability actions.

Two ways to reply today:
1. A workflow that polls then sends to a known `chat_id` (static or single-recipient).
2. Manual orchestration where the polled output drives subsequent `send_message` steps.

The general loop lands when the agent runtime gains capability dispatch (a separate, larger piece — same gap noted for any "agent acts with tools" workflow).

## Template: add another channel (Slack, iMessage, ...)

Telegram is the reference for channel capabilities. To add one:

1. **Runtime module** `jigga/runtime/<channel>.py`: token/credential storage in `~/.jigga/secrets/`, offset/cursor state in `~/.jigga/channels/`, `poll_messages(home, discover=...)` returning the normalized shape above (allowlist-filtered, default-deny), `send_message(home, chat_id, text)`, and a `<channel>_handler` dispatching `<channel>.poll_messages` / `<channel>.send_message`. In-process HTTP — native-action category.
2. **Optional-capability package** `jigga/optional_capabilities/<channel>/`: `manifest.yaml` (`handler: runtime.<channel>`, actions, network/secrets perms, `risk_level: medium`) + `__init__.py` with a `setup(paths, *, input_fn, print_fn, ...)` wizard (collect token, optional discovery, write `channels.<channel>` config). Parameterise I/O for tests.
3. **Register**: add to `REGISTRY` in `jigga/optional_capabilities/__init__.py` and `dispatcher.HANDLERS["runtime.<channel>"]`.
4. **CLI (optional)**: `jigga <channel> status/discover/logout`.
5. **Tests**: mock `urllib`/the API client; cover poll parsing + allowlist (incl. default-deny + discover bypass) + offset advance + send + not_connected + handler dispatch + setup wizard.

## Follow-up work

- Agent-runtime capability dispatch → true auto-reply loop.
- Push-based gateway (webhook receiver) for channels that support it, layered over the supervisor.
- Per-message reply convenience: a `telegram.reply` action that takes a polled message and answers its `chat_id` in one step.
- Slack and iMessage channels using the template above.
- Richer message types (attachments, buttons) beyond plain text.

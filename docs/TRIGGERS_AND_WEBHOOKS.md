# Triggers and Webhooks

How a workflow gets started, and how to expose an authenticated HTTP endpoint
that starts one.

There are three sources, and they deliberately share one path:

| Source | Kind | Evaluated | Declared as |
|---|---|---|---|
| **Time** | pull | supervisor heartbeat | `trigger.schedule` |
| **State** | pull | supervisor heartbeat, by asking a capability | `trigger.event` |
| **Push** | async | webhook listener → durable queue → heartbeat | `trigger.webhook` |

Time and state are the same mechanism — a state trigger just consults a
capability instead of a clock. Push arrives asynchronously, but is **queued**
and executed on the same heartbeat, so all three converge on one execution path
instead of growing separate ones that drift apart.

---

## Quick start: webhooks

Three steps. The listener will not start until all three are done.

**1. Issue a key to each third party.**

JIGGA is the *provider* here: it mints a credential and you paste it into the
caller's dashboard. Issue one **per caller**, not one shared key — that is what
makes a caller nameable in the audit log, revocable on its own, and restrictable
to its own workflows.

```bash
jigga webhook issue postiz
```

```
Issued a webhook key for 'postiz'. Give this to them — it is not shown again:

  1pLMJfxdg2uTjCNekF29Y-Ho5NGxTnpSCPWTiHjYFJQ

They should send it as:  Authorization: Bearer <key>
```

The value is printed **once** and then only ever lives in the secrets broker;
re-printing a stored credential on demand would copy it into every shell history
and terminal scrollback. If it is lost, revoke and issue a new one.

```bash
jigga webhook list      # which callers have keys (names only)
jigga webhook status    # enabled? for whom? shared key set?
jigga webhook revoke postiz
```

Revoking one caller leaves every other integration working — the reason not to
share a single key.

<details>
<summary>Single shared key (legacy)</summary>

`jigga secrets set webhook_api_key` still works and is honoured as a fallback,
but requests using it are attributed to `shared` rather than to a named caller,
and it cannot be revoked per integration. For an unattended install, use the
`env` secrets backend and export `JIGGA_SECRET_WEBHOOK_KEY@<CALLER>`.
</details>

**2. Enable the listener** in `~/.jigga/config.yaml`:

```yaml
webhook:
  enabled: true          # off by default — opening a socket is never implicit
  bind: 127.0.0.1        # or your tailnet IP; 0.0.0.0 is opt-in and deliberate
  port: 8899
  max_body_bytes: 65536  # 64 KiB default
```

**3. Opt a workflow in.** A pushed event can only run a workflow that declares
a matching `webhook:` trigger — the sender never picks the target.

```yaml
id: publish_result
name: Handle a publish result
trigger:
  webhook: publish_result     # must equal the <kind> in the URL path
  source: postiz              # optional: only this caller's key may start it
nodes:
  - id: record
    type: tool
    agent: ops
    action: filesystem.write_file
    input:
      path: ~/notes/publishes.md
      content: "${trigger.status}"
```

Restart the supervisor so it picks up the listener (`jigga service stop && jigga service start`,
or `jigga update --apply`), then post to it:

```bash
curl -X POST http://127.0.0.1:8899/hooks/publish_result \
  -H "Authorization: Bearer $JIGGA_WEBHOOK_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: delivery-42" \
  -d '{"workflow": "publish_result", "status": "published"}'
```

```json
{"status": "accepted", "id": "inevt_..."}
```

Verify it was accepted with `jigga audit --type event.received`, and that the listener came
up with `jigga audit --type webhook.listening`.

### Responses

| Code | Meaning | What the sender should do |
|---|---|---|
| `202` | Accepted and queued | Nothing — it will run on the next tick |
| `200` | Duplicate delivery | Nothing; already handled. Do **not** retry |
| `400` | Body is not a JSON object | Fix the payload |
| `401` | Missing or wrong key | Fix the credential |
| `404` | Unknown path | Use `/hooks/<kind>` |
| `413` | Body over `max_body_bytes` | Send less |
| `503` | Queue full | Retry after `Retry-After` seconds |

`202` means **queued, not executed.** The run happens on the next supervisor
tick. If you need the result, have the workflow write it somewhere or notify a
channel — the HTTP response deliberately does not wait.

### Security model

The listener is JIGGA's only inbound network surface, so it does as little as
possible: authenticate, enqueue, return.

- **Off by default.** Adding a listening socket to a machine is not implicit.
- **No key, no listener.** With no `webhook_api_key` stored, the server
  *refuses to start* rather than serving anonymously. Check
  `jigga audit --type webhook.not_started` if you enabled it and nothing is
  listening.
- **Constant-time key comparison** — a plain `==` leaks the key's prefix
  through timing.
- **Bounded body**, rejected on `Content-Length` before reading.
- **`127.0.0.1` by default.** Binding wider is a deliberate config change.
- **Rejected keys are audited without the presented value** — a near miss is
  often a real key with a typo, and logging it would be the leak.
- **The sender cannot choose the target.** Targeting is enforced at the queue
  drain, not in the listener, so even a compromised listener cannot start an
  arbitrary workflow. An event for a workflow that did not opt in is parked in
  `~/.jigga/events/failed/`, never run.
- **One key per caller.** The authenticated caller's name is recorded on every
  event, so a workflow can require `source:` and one integration's key cannot
  start another's. `source` is always the identity JIGGA authenticated — never
  anything the request claimed about itself.

Payloads are never trusted. They reach a workflow only as `${trigger.*}`
references, which **fail closed** — a typo'd `${trigger.tilte}` raises rather
than silently rendering as literal text.

### Why it queues instead of running inline

The listener writes a file and returns; the supervisor runs it on the next tick.
That buys four things at once:

- **Fast response** — every provider times out, and a slow endpoint manufactures
  duplicate deliveries
- **Crash safety** — the event is durable before the sender is told OK
- **Bounded concurrency** — execution stays inside the tick budget, so a burst
  cannot fan out unbounded
- **One execution path** — shared with scheduled and event-triggered runs

### Delivery, duplicates, and failure

Webhook delivery is **at-least-once** everywhere, so dedup is required, not
optional. JIGGA takes the idempotency key from the first of
`X-Idempotency-Key`, `X-Delivery-Id`, `X-GitHub-Delivery`, `X-Request-Id`, and
falls back to a SHA-256 of the body. Keys are remembered for 48 hours, so a
provider retrying long after the run completed still will not re-fire.

A claimed event is moved to `events/processing/` **before** it runs. If the
process dies mid-run it does not silently replay on restart — a half-executed
side effect (a message half-sent, an order half-placed) must not be blindly
repeated. Stranded claims are swept into `events/failed/` where they are visible
and you decide.

```bash
jigga audit --type event.failed        # what failed and why
ls ~/.jigga/events/failed/             # the parked payloads
```

Nothing is ever re-queued automatically.

### Tuning

```yaml
events:
  max_pending: 500              # queue depth; beyond this the listener answers 503
  processing_stale_minutes: 60  # a claim older than this is treated as crashed
```

---

## Time triggers

Workflows accept **full 5-field cron** or a friendly form. Both are evaluated on
the supervisor heartbeat; the OS scheduler only keeps the supervisor alive.

```yaml
trigger:
  schedule: "0 10 20 * *"        # day 20 of the month at 10:00
```

```yaml
trigger:
  schedule: "weekdays at 09:00"  # also: "daily 9:00", "weekend 10am", "every day at 6:30pm"
```

Cron supports ranges, steps, lists, and day/month names (`0 9 * * MON-FRI`).
A schedule that parses as neither form is reported by `jigga validate` rather
than silently never firing.

Agents use the same cron syntax under `wake.schedules[].cron`.

---

## Event (state) triggers

Fire because something is *true*, not because the clock said so.

```yaml
trigger:
  event: calendar_event_upcoming
  offset: 15m                    # also 2h, 1d; bare numbers are minutes
  agent: meeting_prep_agent      # REQUIRED — whose credentials evaluate this
  match: {calendar: work}        # optional; every declared key must match
```

**`agent:` is required and has no default.** Evaluating the trigger reads
somebody's calendar, so it must say whose. Omitting it is a reported error
(`jigga audit --type workflow.trigger_error`), not a guess.

Firing is **per-subject**: "15 minutes before any meeting" is true for the whole
window, so each specific meeting fires exactly once rather than once per tick.
Subjects are remembered for 72 hours.

The firing event is available to every node as `${trigger.<field>}`, or
`${trigger}` for the whole payload.

Adding a new event type means adding an evaluator to `EVENT_EVALUATORS` in
`jigga/runtime/triggers.py`. It returns the *subjects* that currently satisfy
the trigger; dedup and event construction are handled once for all of them.

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Enabled the listener, nothing is bound | `jigga audit --type webhook.not_started` — almost always a missing API key |
| `401` on every request | `jigga secrets list` for `webhook_api_key`; confirm the `Bearer ` prefix |
| `202` but nothing runs | The workflow must declare `trigger.webhook: <kind>` matching the URL. Look in `~/.jigga/events/failed/` |
| Event trigger never fires | `jigga audit --type workflow.trigger_error` — usually a missing `agent:` or an unknown event name |
| Scheduled workflow never fires | `jigga validate` — an unparseable schedule is now reported |
| A run fired many times | Should not happen; per-subject dedup covers it. Check `event_fired` in `~/.jigga/loop_state.json` |

## Related

- [`WORKFLOWS.md`](WORKFLOWS.md) — workflow shape, steps, approvals
- [`WORKFLOW_ENGINE_V2_RUNTIME_NOTES.md`](WORKFLOW_ENGINE_V2_RUNTIME_NOTES.md) — the DAG engine
- [`FIELD_LESSONS_HMX_PRODUCTION.md`](FIELD_LESSONS_HMX_PRODUCTION.md) — the outages this design is shaped by

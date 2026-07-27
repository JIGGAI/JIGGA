# Observability Runtime Notes (Milestone C)

The runtime writes a JSONL audit event at every trust boundary. Milestone C adds the **read** side — the ability to see what happened — plus **secret redaction** so the durable log never captures credentials, and **trace-id propagation** so one id returns a whole causal tree. Cost/budget tracking and log rotation have since shipped too (documented below) — Milestone C is complete.

## Secret redaction (always on)

`audit.append_event` now runs every detail value through `redact()` before writing:

- **Key-based:** any detail under a sensitive key name (`token`, `password`, `secret`, `api_key`, `authorization`, `credential`, `access_token`, `refresh_token`, `bot_token`, `client_secret`, `private_key`, ...) is replaced with `***redacted***`, recursively.
- **Value-based:** any string (anywhere, including inside error messages) has known secret shapes pattern-replaced — OpenAI `sk-…`, GitHub `ghp_…`, Slack `xox…`, AWS `AKIA…`, Telegram bot tokens (`<digits>:<hash>`), and `Bearer <token>` headers.

Conservative by design: ordinary prose (numbers, words, short ids) passes through untouched. It's a defensive net against a capability echoing a credential into an error/detail field — not a substitute for not logging secrets in the first place.

## Reading the log

Three commands, all over `~/.jigga/logs/events.jsonl`:

```bash
jigga logs tail [-n N] [--json]              # most recent N events
jigga audit [--agent X] [--type T] \         # filtered query
            [--since 30m|24h|7d|ISO] \
            [--status ok|error|deny|ask] \
            [-n N] [--json]
jigga trace <id> [--json]                    # everything correlated to an id
```

- `--type` matches an exact type or a **family prefix**: `--type agent.run` matches `agent.run.started` and `agent.run.completed`; `--type agent.tool_call` matches all four tool-call events.
- `--since` accepts relative durations (`30m`, `24h`, `7d`, `2w`, `90s`) or an ISO timestamp.
- `--agent` matches `agent` / `agent_id` / `parent_agent_id` in the event details.
- Human output is one line per event: `<time> [status] <type> <key details>`. `--json` emits the raw events.

### `jigga trace <id>`

Correlates by any id-shaped value an event carries — its own `id`, or `trace_id` / `run_id` / `task_id` / `session_id` / `workflow` / `agent` in details — matched exactly or by prefix. So `jigga trace <run_id>` stitches together a whole agent run (started → tool calls → task completed → run completed), and `jigga trace <task_id>` follows one task across runs.

### Trace-id propagation

Every audit event carries an ambient `trace_id`. It's bound by a `ContextVar` (`audit.trace_context`) at the runtime's entry points — `supervisor_tick`, `run_agent`, `run_workflow`, and the channel `ingest_once` cycle — and **inherited** by anything they call rather than re-minted. Because a tick, the agent runs it wakes, the workflow steps they execute, and the subagents they spawn all run on one thread in one process, the id threads through every `append_event` without each call site passing it. A standalone `run_agent` / `run_workflow` (CLI) mints its own.

So `jigga trace <trace_id>` returns the **whole causal tree from a single id** — supervisor tick → agent run → tool calls → spawned subagent — while the narrower `run_id` / `task_id` still scope to a single run or task. Run records and task artifacts under `~/.jigga/runs/` also carry `trace_id`, so you can jump from a stored run straight to `jigga trace`.

## Cost tracking & budgets

Every model call records token usage and dollar cost on its `model.call` audit event. `call_model` is the single chokepoint for model spend, so that's where pricing and enforcement live.

- **Usage:** real providers report exact `prompt`/`completion` tokens; dry-run and providers that omit usage are estimated (~4 chars/token).
- **Pricing is config** under `models.pricing` — per-model `input_per_1k` / `output_per_1k` rates, with a `default` fallback and free (0.0) when a model isn't priced:

  ```yaml
  models:
    pricing:
      gpt-4o:  {input_per_1k: 0.005, output_per_1k: 0.015}
      default: {input_per_1k: 0.0,   output_per_1k: 0.0}
  ```

- **Budgets are opt-in** under `budgets` — a per-agent or `default` `limit_usd` over a rolling `window` (default `30d`; `all` for no time bound):

  ```yaml
  budgets:
    window: 30d
    agents:
      marketing_lead: {limit_usd: 10.0}
    default: {limit_usd: 5.0}
  ```

  Enforcement: when an agent has already spent its whole cap, the next call is **hard-stopped** — `call_model` returns an error result and emits `budget.exceeded` (`status=deny`); the agent loop surfaces that as a failed run rather than silently overspending. On the way up, the call that first crosses 80% emits `budget.warning` (`status=ask`) exactly once.

`jigga cost` reads it all back:

```bash
jigga cost                       # per-agent rollup + budget status, all time
jigga cost --since 7d            # this week
jigga cost --agent marketing_lead --json
```

```
agent                     calls   in_tok  out_tok      cost  budget
marketing_lead                2      146       46   $7.1400  $7.14/$6.00 (119%) ⛔
copywriter                    1       27       23   $2.1900  $2.19/$2.00 (110%) ⛔
seo_analyst                   1        3       23   $1.4700  $1.47/$2.00 (74%)
total                         4      176       92  $10.8000
```

> **Note:** spend-to-date is computed by scanning `events.jsonl` on each model call (O(events)). Fine at current scale; a running per-agent ledger is the obvious optimization if the log grows large — tracked with log rotation below.

## Log rotation & retention

`events.jsonl` is append-only and would otherwise grow forever. The supervisor rolls it over on its heartbeat (`supervisor_tick` → `rotate_logs`), so the write path (`append_event`) stays free of config loads and file stats — the common tick is a `stat` and nothing else.

- **Rollover** when the active log crosses a calendar day *or* a size cap, into a dated archive `events-YYYY-MM-DD.jsonl` (same-day size splits get a `.N` suffix).
- **Retention:** dated archives older than `retention_days` are pruned right after a rollover (not on every tick).
- **Readers fold archives back in:** `read_events` reads the dated archives (oldest first) then the active log, so `jigga audit` / `trace` / `cost` and the **budget windows** still see history across a rollover — a 30-day budget isn't reset by a daily rotation.

```yaml
logs:
  rotation:
    enabled: true
    max_bytes: 10485760     # 10 MiB
    retention_days: 30
```

```bash
jigga logs rotate            # force a rollover + prune now (otherwise automatic)
```

> Because readers concatenate all retained archives, a query over a long window does more file I/O as history grows. The running per-agent cost ledger noted above is the natural next optimization riding on this archive structure.

## Example

```bash
# what did this agent do in the last hour?
jigga audit --agent daily_briefing_agent --since 1h

# everything that was denied or needs approval today
jigga audit --status deny --since 24h
jigga audit --type agent.tool_call.needs_approval --since 24h

# follow one agent run end to end
jigga trace agent_run_f312fd56539d

# what did the team cost me this week?
jigga cost --since 7d
```

## Status

Milestone C is complete: audit query CLI + secret redaction, trace-id propagation, cost tracking + per-agent budgets, and log rotation + retention all shipped. The one remaining optimization (not a feature gap) is a **running per-agent cost ledger** so spend isn't recomputed by scanning the log on each model call — a natural follow-up on the archive structure above, worth doing if model-call volume grows.

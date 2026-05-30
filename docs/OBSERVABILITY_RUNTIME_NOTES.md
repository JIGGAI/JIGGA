# Observability Runtime Notes (Milestone C)

The runtime writes a JSONL audit event at every trust boundary. Milestone C adds the **read** side — the ability to see what happened — plus **secret redaction** so the durable log never captures credentials, and **trace-id propagation** so one id returns a whole causal tree. Cost/budget tracking and log rotation are the remaining follow-up PRs (tracked in `ROADMAP_TO_PRODUCTION.md`).

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

## Example

```bash
# what did this agent do in the last hour?
jigga audit --agent daily_briefing_agent --since 1h

# everything that was denied or needs approval today
jigga audit --status deny --since 24h
jigga audit --type agent.tool_call.needs_approval --since 24h

# follow one agent run end to end
jigga trace agent_run_f312fd56539d
```

## Follow-up work (rest of Milestone C)

- **Cost tracking + budgets** — record per-model-call cost (tokens × provider rate), roll up per agent/workflow, soft-warn at 80% / hard-stop (`policy.denied`) at 100% of a configured cap.
- **Log rotation + retention** — daily rollover of `events.jsonl` with a configurable retention window (the log grows unbounded today).

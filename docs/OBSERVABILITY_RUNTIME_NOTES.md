# Observability Runtime Notes (Milestone C)

The runtime writes a JSONL audit event at every trust boundary. Milestone C adds the **read** side — the ability to see what happened — plus **secret redaction** so the durable log never captures credentials. This is the first slice; trace-id propagation, cost/budget tracking, and log rotation are separate follow-up PRs (tracked in `ROADMAP_TO_PRODUCTION.md`).

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

Correlates by any id-shaped value an event carries — its own `id`, or `run_id` / `task_id` / `session_id` / `trace_id` / `workflow` / `agent` in details — matched exactly or by prefix. So `jigga trace <run_id>` stitches together a whole agent run (started → tool calls → task completed → run completed), and `jigga trace <task_id>` follows one task across runs.

Until **trace-id propagation** lands (follow-up), there's no single parent id spanning supervisor-tick → agent-run → spawned-subagent; `trace` correlates by the ids that already exist, which covers most chains. The propagation PR will thread one `trace_id` through so a single id returns the full causal tree.

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

- **Trace-id propagation** — one `trace_id` threaded supervisor-tick → agent-run → tool-call / subagent so `trace` returns the full tree from a single id.
- **Cost tracking + budgets** — record per-model-call cost (tokens × provider rate), roll up per agent/workflow, soft-warn at 80% / hard-stop (`policy.denied`) at 100% of a configured cap.
- **Log rotation + retention** — daily rollover of `events.jsonl` with a configurable retention window (the log grows unbounded today).

# Agent-Runtime Capability Dispatch — Design

The keystone that turned JIGGA agents from *summarizers* into *actors*. (This
design has shipped — `run_agent` is the bounded tool-use loop described below;
the motivation that follows is written from the pre-change world.) Before this
change, `run_agent` did: model call → write text artifact → mark task done. The
model's output was just text — the agent couldn't call `telegram.send_message`,
`filesystem.write_file`, `gog.gmail_draft`, or `spawn_subagent`. Every real
action had to be a hardcoded workflow step. This design lets the model decide to
call capabilities, dispatches them through the existing capability layer (with
the same policy/approval/audit as workflow steps), feeds results back, and
loops until done.

It unblocks everything autonomous — channel auto-reply, autonomous email/
calendar work, real delegation.

## The loop

For a woken agent with a task:

1. Resolve the agent's **allowed actions** (see "Allowlist" below) and build a
   tool schema from the matching capabilities.
2. Call the model with the task + tool schema (system: role + instructions +
   scoped memory; user: task).
3. Model returns either a **final text answer** (done) or **tool-call
   requests**.
4. For each tool call → `dispatch_action(...)` through the capability layer
   (same policy + risk gating + approval + audit as a workflow step), append
   the result to the conversation, loop.
5. Bounded by `max_tool_calls_per_run` and `max_iterations`.

## Allowlist resolution (decided 2026-05-29)

`AgentConfig.tools` (which exists today and is currently unused) becomes the
base allowlist of action names the agent may call. Agent `permissions`
override/extend on top:

- Base set = `agent.tools`.
- Per-call, `permissions` still gate every invocation at dispatch time
  (`permission_mode`, filesystem/network/resource policy, capability
  `risk_level`). Permissions can therefore *restrict* below the `tools` list.
- A permissions mechanism may also *add* actions (e.g. a future
  `permissions.tools.allow`). Resolution: effective = (`agent.tools` ∪
  permission-added) then filtered by per-call policy.

The point: `tools` is the explicit, auditable allowlist; permissions are the
enforced gate. An action not in the effective set is never offered to the model.

## Safety — the crux

Every tool call routes through the **same gates a workflow step hits**:

- `permission_mode`: `plan_only` / `locked_down` → no execution at all.
- `evaluate_capability_permissions` + per-resource policy (filesystem/network/...).
- Capability `risk_level` approval (medium/high).
- New audit events: `agent.tool_call.requested` / `.executed` / `.denied` so the
  trace shows what the agent did and why.

**`ask`-mode mid-loop with no human present (daemon context):** record the call
as `needs_approval` and **halt that task** rather than block waiting. (Decided
2026-05-29.)

**Bounds (decided 2026-05-29):** default `max_tool_calls_per_run: 10`,
`max_iterations: 8`, overridable in `config.yaml`. Same loop-prevention
philosophy as `loop_guard`.

## Phasing — 3 reviewable PRs

### PR A — extract `dispatch_action` core (this PR) ✅
Pure refactor: pull the resolve-capability → emit `capability.invocation.*` →
resolve-handler → invoke path out of `execute_step` into a standalone
`dispatch_action(step, resolved_input, memory_context, runtime, registry,
logs_dir, *, run_id, workflow_id=None)`. `execute_step` now calls it and keeps
only the workflow-specific `resolve_value(outputs)` + artifact writing. One code
path for *all* capability invocation → identical policy/audit for workflow
steps and (soon) agent tool calls. No behavior change (artifact path is still
traced via `workflow.step.completed`).

### PR B — tool-calling in `model_router` ✅
Extended the model request/response to pass a tool schema and parse tool-call
responses. `dry_run` provider is scriptable via `dry_run_tool_calls`
(deterministic tool calls for tests); `openai_compatible` passes `tools` and
reads `tool_calls`. Mocked-provider tests.

### PR C — the agent loop ✅
`run_agent` is now a bounded tool-use loop. Per task: resolve the tool
allowlist (`agent.tools` + `permissions.tools.allow`, filtered to
registry-resolvable actions), build OpenAI tool schemas (dot-sanitized names —
`telegram.send_message` → `telegram__send_message` — with an exact reverse
map), call the model with tools, dispatch each requested call via
`dispatch_action` with the per-call gate, feed results back, loop. Bounded by
`max_tool_calls_per_run` / `max_iterations`. Audit:
`agent.tool_call.requested` / `.executed` / `.denied` / `.needs_approval`.

Gate per call (`_gate_tool_call`):
- not in the agent's allowlist → denied (tool-result error, loop continues).
- `evaluate_capability_permissions` deny → denied (tool-result error, continues).
- `risk_level` medium/high and mode ≠ `autonomous` → `needs_approval`, **halt
  the task** (decided 2026-05-29).
- over the call cap → halt.

No-tool / dry-run behavior is preserved exactly (one call → final text →
completed), so agents without `tools` or running against the dry-run provider
behave as before.

## Running against a real model

The loop calls whatever provider is configured — there's nothing dry-run-only
about it. To make agents act with a real LLM:

```yaml
# ~/.jigga/config.yaml
models:
  defaults:
    provider: openai
  providers:
    openai:
      kind: openai_compatible
      base_url: https://api.openai.com/v1
      api_key_env: OPENAI_API_KEY
      default_model: gpt-4o-mini          # or any tool-calling model
  profiles:
    default:
      primary: openai
      fallback: [dry_run]
```

Then `export OPENAI_API_KEY=...`. Any agent with `tools` will now have the real
model decide which capabilities to call; each call still passes through the
same policy/risk/approval gate as a workflow step. An agent can also set
`model: profile:default` or `model: gpt-4o-mini` to override per-agent.

Optional loop bounds:

```yaml
agent_loop:
  max_tool_calls_per_run: 10
  max_iterations: 8
```

## Related track (shipped): channel listeners

Channel inbound is NOT cron-driven (polling every 5s is wasteful and laggy).
The implemented model is a **long-poll listener** (`runtime/channel_listener.py`,
`jigga channels listen`): one blocking `getUpdates`-style call per channel that
waits server-side up to ~30s for a message, turns messages into tasks, and runs
the assigned agent (this keystone's loop) so it can reply. This is the
efficiency/latency layer; the capability-dispatch keystone above is what lets
the agent actually *reply*. Together they form the full receive → think → reply
loop. See `docs/CHANNELS_TELEGRAM_RUNTIME_NOTES.md`.

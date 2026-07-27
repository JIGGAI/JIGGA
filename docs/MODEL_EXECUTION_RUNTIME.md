# Model Execution Runtime Notes

JIGGA Phase 4 added workflow inference, not AI/model inference. This note documents the first real model execution boundary added after that distinction.

## What exists now

- `jigga.runtime.model_router` loads model provider config from `~/.jigga/config.yaml`.
- Default runtime uses a safe `dry_run` provider, so tests and demos do not require credentials.
- OpenAI-compatible chat-completions providers are supported via env-var credentials.
- **ChatGPT-subscription provider** (`chatgpt_oauth`) — run on a ChatGPT Plus/Pro subscription with no API key; see `CHATGPT_OAUTH_PROVIDER.md`.
- **Rate-limit resilience** (PR #146) — per-provider min-spacing, a 429 circuit breaker honoring Retry-After, and automatic provider fallback via `profiles.<id>.fallback`; see `jigga/runtime/model_throttle.py` and `tools/MODEL_ROUTER_FALLBACKS.md`.
- Agent task execution now routes through the model router and writes model call artifacts under `runs/agents/...`.
- `jigga model test <agent_id> --prompt ...` exercises the model boundary.

## Config shape

```yaml
models:
  defaults:
    provider: dry_run
  providers:
    dry_run:
      kind: dry_run
      default_model: dry-run
    openai:
      kind: openai_compatible
      base_url: https://api.openai.com/v1
      api_key_env: OPENAI_API_KEY
      default_model: gpt-4o-mini
      timeout_seconds: 60
  profiles:
    default:
      primary: openai
      fallback:
        - dry_run
```

Agent configs may set `model: profile:default` to select a profile or `model: gpt-4o-mini` to override the provider default model.

## Duplicate item prevention

Provider item IDs are treated as provider metadata, not JIGGA's canonical session IDs. Before any model call, JIGGA validates the outbound input package and rejects duplicate item IDs locally.

This prevents provider errors like:

```text
Duplicate item found with id msg_289
```

Future Responses API continuation support should keep `previous_response_id` state inside the model router/session boundary. If continuation state becomes invalid, JIGGA should reset provider state and retry once without `previous_response_id` rather than replaying mixed old/new provider item IDs.

## CLI

```bash
jigga model test daily_briefing_agent --prompt "Summarize my day" --dry-run
jigga run agent daily_briefing_agent --dry-run-model
```

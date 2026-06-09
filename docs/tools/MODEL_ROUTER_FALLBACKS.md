# Model Router & Fallbacks

## Purpose

Different agents and tasks benefit from different models. JIGGA should route work to the best available model while supporting user preference, cost limits, privacy constraints, and fallback behavior.

## Product Definition

The **Model Router** selects a model/backend for each agent run based on task type, agent config, availability, cost, latency, and privacy policy.

## Config Example

```yaml
models:
  defaults:
    reasoning: gpt-5.5
    coding: claude-code
    fast: local-small
  fallbacks:
    gpt-5.5:
      - claude-sonnet
      - local-large
  policies:
    private_memory:
      allow_cloud: false
```

## Agent Example

```yaml
agent:
  id: reviewer
  model_profile: coding_review

model_profiles:
  coding_review:
    primary: claude-code
    fallback:
      - codex_cli
      - gpt-5.5
    max_cost_per_run: 2.00
```

## Selection Inputs

- Task type
- Required tools
- Memory sensitivity
- Context size
- Cost budget
- Latency budget
- User preferences
- Provider health

## APIs

```ts
interface ModelRouter {
  select(input: ModelSelectionInput): Promise<ModelSelection>
  recordFailure(model: string, reason: string): Promise<void>
  estimateCost(input: ModelSelectionInput): Promise<CostEstimate>
}
```

## Rate-limit resilience (implemented — #83/#84/#85)

`call_model` defends against provider rate limits (the ChatGPT-subscription
cap, an API tier limit) on three fronts, all configured under `models:`:

```yaml
models:
  defaults:
    # #83 — client-side spacing: never fire a real provider more often than
    # this many seconds apart (per provider, persisted across ticks/processes
    # in state/model/throttle.json). 0 / unset = disabled.
    min_seconds_between_calls: 2
    # #84 — circuit breaker: after this many consecutive 429s a provider is
    # parked for the cooldown, so a sustained cap isn't hammered.
    rate_limit_threshold: 3
    rate_limit_cooldown_seconds: 60
  providers:
    chatgpt:
      kind: chatgpt_oauth
      min_seconds_between_calls: 3   # per-provider override of the default
  profiles:
    default:
      primary: chatgpt
      fallback: [openai]             # #85 — used automatically on a 429
```

- **#83 Client-side spacing** — `call_model` waits out `min_seconds_between_calls`
  before hitting a provider, so JIGGA's own bursts don't trip the cap.
- **#84 Smarter 429 backoff** — the in-call retry honors a `Retry-After` header,
  is kept short (2) with jitter, and after `rate_limit_threshold` consecutive
  429s the provider's **circuit breaker** opens for `rate_limit_cooldown_seconds`
  (subsequent calls skip it entirely rather than re-pinning the cap).
- **#85 Provider fallback** — a 429 that exhausts retries raises a typed
  `RateLimitError`; `call_model` then advances to the next provider in
  `profiles.<id>.fallback` (e.g. an OpenAI API key, or a local OpenAI-compatible
  endpoint). The result is marked `fallback_used`. A `model.rate_limited` audit
  event records each cooldown/skip.

## Safety Rules

- Do not send sensitive local memory to cloud models when policy forbids it.
- Log model used for each session.
- Surface fallback events to the user when they materially affect quality/cost/privacy.
- Keep provider credentials outside capability packs.

## V1 Build Tasks

- Implement static model profiles.
- Add fallback list.
- Add cost/latency metadata.
- Add privacy flag: `allow_cloud`.

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

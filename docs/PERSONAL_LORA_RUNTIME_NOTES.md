# Personal LoRA Runtime Notes

Sketch / v0 scaffolding for a hybrid memory architecture: keep facts in the existing scoped-memory kernel where they are inspectable, deletable, and updatable; bake **voice, vocabulary, and decision patterns** into a parameter-efficient (LoRA) adapter applied on top of a base model. The two layers compose at the agent boundary.

**Status as of this branch:** scaffolding only. Manifest, handler module, optional-capability registration, and CLI plumbing are in place. The actions return `status: "planned"` payloads that describe what the v1 implementation will do. No training, no model loading, no weight surgery yet — that is the next agent's job. See *Next Agent Picking This Up* at the bottom.

---

## Why this exists

JIGGA's memory kernel is strong at exactly the things a fine-tuned model is bad at: atomic updates, scoped retrieval, sensitivity gating, right-to-delete. It is weak at exactly the things a fine-tuned model is good at: making an agent sound like the user, absorbing diffuse preferences, picking up vocabulary and decision style without an explicit fact written somewhere.

A pure-retrieval system reads context every turn but never *internalizes* the user. A pure-LoRA system internalizes everything but cannot atomically update a fact, scope what a given agent sees, or honor a delete request without retraining. Neither solves the personal-AI problem alone.

The split this module is designed around:

| Concern | Owner |
|---|---|
| Facts, episodes, time-stamped events | `memory_scope` (existing) |
| Voice, vocabulary, decision patterns | personal LoRA (this module) |
| Fresh state since last training | `memory_scope` |
| Implicit preferences hard to enumerate | personal LoRA |
| Sensitive data with delete requirements | `memory_scope` |
| Style that should always be on without retrieval | personal LoRA |

Memory is *explicit context*; the LoRA is *implicit prior*. They compose:

```
base_model + LoRA(voice prior)          ← implicit, always-on per agent
            + memory_scope(context)     ← explicit, this-task-only
            + task prompt
```

---

## Tier

Lives under `jigga/optional_capabilities/personal_lora/` — the **opt-in first-party tier**, same as `google-calendar`, `telegram`, `gog`. Not bundled because:

- Training infrastructure (GPU, datasets, eval harness) is not something every user wants.
- The risk surface (modifies model behavior for downstream agents) demands explicit install + approval.
- Most users will start with retrieval-only memory and opt into a LoRA layer only once they have enough writing/notes to train on.

Install with `jigga capabilities install personal-lora`. The setup function creates `~/.jigga/loras/` and seeds an example `training_scopes.yaml`.

---

## Where it slots in `model_router`

The model router already has a profile concept (provider + fallback list). A LoRA is just **another field on a profile** — not a parallel system:

```yaml
# ~/.jigga/config.yaml (target shape for v1)
models:
  providers:
    local_llama:
      kind: local_transformers          # new provider kind, v1 work
      base_model: meta-llama/Llama-3.1-8B-Instruct
    openai:
      kind: openai_compatible
      api_key_env: OPENAI_API_KEY
      default_model: gpt-4o-mini
  loras:
    voice_v3:
      path: ~/.jigga/loras/voice/v3
      base_model: meta-llama/Llama-3.1-8B-Instruct
      training_run: lora_run_abc123
  profiles:
    voice:
      primary: local_llama
      lora: voice_v3                    # <- new field
    default:
      primary: openai
      # no lora — neutral factual recall
```

Per-agent the user chooses:

- `model: profile:voice` for `linkedin_writer` → speaks in the user's voice.
- `model: profile:default` for `daily_briefing_agent` → neutral, factual.

The agent never knows or cares that a LoRA is applied; the router resolves the adapter and the local provider mounts it. This is the key architectural seam — **adding LoRA support does not require any change to agent.py, workflow.py, or dispatcher.py**. It is a model_router concern.

---

## Local-provider load and cache strategy

A loaded 8B base model + LoRA is heavy on memory and slow to cold-start. The local provider needs an in-process cache or the runtime is unusable in practice.

**Cache shape.** Keep one resident model per `(base_model, quantization)` pair. Multiple LoRAs that share a base attach as adapters to the same loaded model and switch via `peft.PeftModel.set_adapter()` — cheap (no reload). LoRAs against different bases each require their own loaded base.

```
local_provider state:
  loaded_bases: {
    "meta-llama/Llama-3.1-8B-Instruct@int4": <PeftModel instance>,
  }
  attached_adapters: {
    "meta-llama/Llama-3.1-8B-Instruct@int4": ["voice_v3", "voice_v4"],
  }
  active_adapter_per_base: {
    "meta-llama/Llama-3.1-8B-Instruct@int4": "voice_v3",
  }
```

**Memory budget.** Llama 3.1 8B at int4 is ~5GB resident; bf16 is ~16GB. Default to int4 for the local provider; expose a `quantization: bf16|int4|int8` knob per provider config. A new `models.local_provider.max_resident_bases: 1` config knob caps memory pressure — exceeding it evicts the least-recently-used base.

**Cold start.** Lazy-load on first invocation. Emit `model.local.warming` audit event before the load and `model.local.warmed` after, with duration. Subsequent invocations against the same base + adapter combo are warm.

**Lifecycle within a supervisor tick.** Each `agent.run` today instantiates fresh runtime structures but does not fork a process — the cache lives in the supervisor process for the duration of `supervisor_loop`. A `jigga run agent <id>` (one-shot CLI) pays a cold start every time; a long-running `jigga supervisor start` warms once and reuses. Document this trade-off — users running ad-hoc commands against local LoRAs will hit cold starts they would not see under the daemon.

**Future helper-process option.** If cold start cost dominates one-shot UX, the implementation agent can split the local provider into a small persistent helper (think `vllm`-style daemon) that the runtime talks to over a UNIX socket. Out of scope for v1, but the provider interface should not assume in-process; pass an opaque handle (string or socket path), do not hand the runtime a `PeftModel`.

**Hot-swap during a run.** Pin the adapter at the start of an `agent.run` and do not switch mid-run. If the user activates a new version while a run is in flight, the run completes against the old version; the next run picks up the new. This is the only safe semantics — switching adapters mid-generation produces undefined behavior.

---

## The four actions

| Action | What it does | v0 status |
|---|---|---|
| `lora.list` | List adapters present on disk under `~/.jigga/loras/`. | implemented (cheap, no training) |
| `lora.train` | Curate corpus from memory under a `training_scope`, augment, run a sandboxed trainer (Unsloth/Axolotl), produce a new versioned adapter. | stub returning a planned-run payload |
| `lora.evaluate` | Replay facts-back recall + a general-capability subset against the adapter; compare to previous version. | stub |
| `lora.activate` | Switch a profile's `lora:` field to point at a specific adapter version. Approval-gated. | stub |

Splitting train from activate is deliberate: training writes an adapter to disk; activation is the moment the adapter starts affecting downstream agents. The user reviews eval results between the two.

---

## Training pipeline (target shape)

```
lora.train(training_scope, base_model, hyperparams) ->
  1. Resolve training_scope -> memory files. The schema mirrors
     memory_scope (same includes/excludes/sensitivity fields) so the user
     already understands it. Lives at ~/.jigga/loras/training_scopes.yaml.
  2. Curate: extract user-authored text from raw + summaries + completed
     task outputs. Skip anything where sensitivity.allow_sensitive is false
     unless the scope explicitly grants it.
  3. Augment: per fact/statement, generate paraphrases and Q&A pairs via
     a model (configured provider). 50-200 variants per source statement
     is the rough target; under-augmented LoRAs confabulate.
  4. Mix in ~30% general instruction data (Tulu/UltraChat slice) to fight
     catastrophic forgetting. Tracked in the training run record.
  5. run_sandboxed("unsloth-train", ...) — authority-side spawner. The
     existing SandboxSpec primitive applies here unchanged. Local GPU only
     in v1; offload providers (Modal/Together/Replicate) land as opt-in
     later behind an explicit network + cost permission.
  6. Auto-eval: facts-back recall + an MMLU subset (or similar) to detect
     capability regression. Emit lora.eval.passed or lora.eval.regressed
     with the deltas.
  7. Land adapter at ~/.jigga/loras/<name>/v<N>/ with manifest.json
     capturing: corpus_hash, base_model, hyperparams, eval_scores.
  8. Return the training_run record + eval scores. DOES NOT auto-activate.
```

Activation (`lora.activate <name> v<N>`) is the second gate. It mutates `config.yaml` to swap which version a profile points at, runs through the existing plan/apply approval flow, and emits `lora.activated`. A regression beyond a configurable threshold refuses activation without `--accept-regression`.

### Augmentation: a worked example

Step 3 of the pipeline — augmentation — is the make-or-break step. Under-augmented LoRAs confabulate; over-augmented LoRAs overfit to the augmentation provider's voice instead of the user's. Concrete shape so the next agent has a target to build against.

**Source row** (from `~/.jigga/memory/summaries/preferences.md`):

> "I prefer narrow PRs — under 400 lines, one concern per PR, even if it means more PRs. Reviewer time is the bottleneck, not author time."

**Augmented variants** (the augmenter generates ~50–200 of these per source row; sampling 5 here):

```yaml
- type: qa_pair
  question: "How should I size my pull requests?"
  answer: "Narrow — under 400 lines, one concern per PR. Reviewer time is the bottleneck, not author time."

- type: qa_pair
  question: "Should this PR be split?"
  answer: "If it crosses 400 lines or touches more than one concern, yes. Reviewer time is the bottleneck, not author time."

- type: paraphrase
  text: "Keep PRs small. Under 400 lines. One concern each. Even if that means more PRs."

- type: contextual
  context: "User is reviewing a PR that touches both the migration runner and the email adapter."
  response: "These are two concerns. Split into two PRs — the migration runner is its own review surface and shouldn't ride on an email-adapter change."

- type: stylistic_seed
  text: "Reviewer time is the bottleneck, not author time."
```

**Training row format** (Llama 3.1 chat template):

```
<|begin_of_text|><|start_header_id|>system<|end_header_id|>

You are a personal assistant that mirrors the user's writing voice and decision style.<|eot_id|><|start_header_id|>user<|end_header_id|>

How should I size my pull requests?<|eot_id|><|start_header_id|>assistant<|end_header_id|>

Narrow — under 400 lines, one concern per PR. Reviewer time is the bottleneck, not author time.<|eot_id|>
```

**Five rules the augmenter must follow** (otherwise the LoRA absorbs the wrong thing):

1. **Q&A pairs use the user's voice in the answer, not the augmenter's voice.** Generate the question freely; constrain the answer to the user's own phrasing wherever possible. A LoRA trained on "the augmenter's idea of how the user would phrase X" will sound like the augmenter, not the user.
2. **Paraphrases preserve specifics.** If the source says "under 400 lines," the paraphrase must not drift to "under 500 lines" or "around 400 lines." Specifics that drift in augmentation become hallucination at inference.
3. **Contextual rows ground the preference in a scenario.** This is how the LoRA learns *when* to apply the preference, not just that it exists.
4. **Stylistic seeds preserve raw phrases.** Verbatim user phrases trained as standalone rows reinforce voice without binding to any specific Q&A.
5. **No augmentation across the sensitivity boundary.** If a source row carries `sensitivity.allow_sensitive: false`, the augmenter must not generate variants that include or paraphrase that content — period. The simplest implementation: pass the sensitivity flag through to the augmenter prompt and reject the entire row at curation time.

The augmenter is a model call routed through `model_router` — it inherits dry-run default, the user's configured providers, and the audit trail. Do not bake in a direct OpenAI dependency.

---

## When training triggers

Three modes, all using existing JIGGA primitives — no new triggering machinery:

- **Manual** — `jigga lora train --scope voice --base llama-3.1-8b`. The "I want it now" path.
- **Scheduled** — a workflow such as `weekly_voice_refresh.yaml` on cron. The supervisor already handles `workflow.schedule_due`; nothing new to build.
- **Threshold-based suggestion** — extend `runtime/inference.py` to emit a new suggestion shape when "user-authored memory tokens since last training > N." Matches the existing **propose, don't auto-activate** rule for workflows. The user reviews and approves before training runs — silent retraining is never acceptable for a layer that mutates model behavior.

---

## How it slots next to `memory_scope`

Two persistence dimensions, not alternatives.

| | `memory_scope` | personal LoRA |
|---|---|---|
| Nature | Explicit context loaded per task | Implicit prior in model weights |
| Update | Atomic, immediate (write a file) | Batch, requires retraining + activation |
| Good at | Facts, episodes, fresh state | Voice, vocabulary, decision style |
| Inspectable | Yes — YAML/Markdown on disk | No — opaque adapter weights |
| Deletable | Yes — delete the file | Only by retraining a new adapter |
| Scope control | Per-agent at inference time | Per-LoRA at training time (via `training_scope`) |
| Failure mode | Misses context for a turn | Confabulates a wrong fact |

A `daily_briefing_agent` running with a voice LoRA still loads `manager_view` memory. The LoRA shapes *how* it writes (vocabulary, tone, what it finds boring vs. interesting). The scope decides *what* it sees (today's calendar, this week's priorities, recurring patterns). They never collide.

---

## Why `risk_level: high`

A bundled or first-party capability ordinarily carries `risk_level: low` (or `medium` for `subagent-delegation`). Personal LoRA is the first capability that ships at `high` because:

- A trained-and-activated LoRA mutates model behavior for **every downstream agent that uses the affected profile**. The blast radius is the agent fleet, not one workflow step.
- The training corpus reads heavily from memory. If a `training_scope` accidentally includes sensitive data, the adapter absorbs it and there is no way to surgically delete it — only retrain.
- Confabulation risk is asymmetric: facts written into weights look fluent and confident even when wrong, unlike retrieval misses which fail visibly.

`risk_level: high` means workflow steps that invoke `lora.*` actions require explicit approval unless the agent is in `autonomous` mode — same gate as `spawn_subagent` carries today.

### Privacy invariant: training-scope containment

The hardest safety question this module raises: **when a LoRA is trained on data X, every agent consuming a profile that uses that LoRA effectively has implicit access to X through the weights — even if the agent's own `memory_scope` explicitly excludes X.** This is the core trade-off of fine-tuning vs. retrieval, and it is not theoretical: LLMs can regurgitate training data under adversarial prompts.

JIGGA's existing scoped-memory model exists precisely so that a research subagent does not see the user's medical notes. A LoRA trained on those notes and consumed by that subagent's profile silently breaks that boundary.

**The activation-time check** (the agent implementing this *must* land it before any real activation flow):

```
For each profile referencing LoRA L:
  Resolve L.training_scope -> set of memory paths P_train.
  For each agent A whose `model:` points at this profile:
    Resolve A.memory_scope -> set of memory paths P_agent.
    If (P_train \ P_agent) is non-empty:
      Refuse activation. Surface the leak:
        "Agent {A.id} (profile {profile.id}, scope {A.memory_scope})
         cannot read {leak_paths} through retrieval, but adapter
         {L.name}/{L.version} was trained on it. Activating would
         grant implicit access through model weights."
```

**Override path.** The user can `--accept-leak` to proceed, but the activation emits a sticky `lora.privacy.override` audit event naming each impacted agent and each leaked path. This is the kind of decision that should be inspectable in `jigga audit` for months afterward, not buried.

**Where this fails open today.** Until activation enforces this rule, the safest default is: do not activate a LoRA against any profile whose agents are scoped tighter than `full_user`. Document this as the v0.5 fallback — a coarse "if the LoRA exists, the agent has at least `manager_view`" rule — until the per-path computation lands.

**Why not enforce at training time instead.** Tempting but wrong: a LoRA might be trained for a profile that doesn't exist yet, or be intended for activation on a profile that gets reconfigured later. The check belongs at the moment the LoRA starts affecting an agent, not the moment it gets created.

---

## Authority vs render — does training go through `runtime.sandbox`?

**Yes — authority side.** A training subprocess acts on external systems (the model on disk), needs only a bounded env, and benefits from the timeout discipline. The next agent should follow the rule in `jigga/runtime/sandbox.py`'s module docstring and spawn the trainer via `run_sandboxed` with a `SandboxSpec` that allowlists only the env vars the trainer needs (`PATH`, `HOME`, `HF_TOKEN` if pulling base weights, `CUDA_VISIBLE_DEVICES` for GPU selection).

The model_router's local-provider inference (the *use* of the LoRA at agent runtime) is different — it runs in-process, not as a subprocess, so the sandbox rule does not apply to it. The sandbox boundary is only around the trainer.

---

## Audit events (planned)

Following the existing `<domain>.<verb>[.<modifier>]` naming pattern:

- `lora.train.planned` — manifest + scope resolved, before sandbox spawn.
- `lora.train.started` — trainer subprocess began.
- `lora.train.completed` — adapter landed on disk.
- `lora.train.failed` — trainer exited non-zero or timed out.
- `lora.eval.passed` / `lora.eval.regressed` — auto-eval verdict, with deltas.
- `lora.activated` — a profile now points at a new adapter version.
- `lora.deactivated` — profile reverted to no-LoRA or previous version.

Trace correlation uses the existing `run_id` / `training_run_id` pattern so `jigga trace <id>` walks across train → eval → activate.

### Cost tracking

Both phases cost real money — augmentation is N model calls (cheap per call but adds up at 50–200 variants × thousands of source rows), training is compute (free locally, billable on offload). Both must emit cost-tagged audit events so Milestone C's per-agent/per-workflow rollups (`docs/ROADMAP_TO_PRODUCTION.md` Milestone C) capture LoRA spend alongside model-call spend.

Event shape:

```yaml
type: lora.cost.recorded
status: ok
details:
  phase: augmentation | training | evaluation
  provider: openai | modal | local_gpu
  input_tokens: 12345          # null for training/eval phases on local_gpu
  output_tokens: 6789          # null for training/eval phases on local_gpu
  wall_clock_seconds: 3624     # null for augmentation
  estimated_cost_usd: 0.42
  training_run: lora_run_abc123
```

For local-GPU training, cost is wall-clock × estimated draw — not exact, but enough for budget caps to fire. New config knob `models.local_gpu_cost_per_hour: 0.0` lets users who own their GPU set it to 0 and offload users set it to provider pricing.

A training run is the kind of action that should hit Milestone C's **hard stop** at 100% budget, not soft warn — the next several hours of work won't help if the user is over budget on the first 30 seconds. Wire `lora.train.started` to consult the budget before spawning the trainer.

---

## Disk schemas

The two JSON files that live under each adapter version directory.

### `~/.jigga/loras/<name>/v<N>/adapter.json`

Written by `lora.train` at the end of a successful run.

```json
{
  "schema_version": 1,
  "name": "voice",
  "version": "v3",
  "base_model": "meta-llama/Llama-3.1-8B-Instruct",
  "base_model_revision": "5206a32",
  "training_run": "lora_run_abc123",
  "created_at": "2026-05-30T12:34:56Z",
  "corpus": {
    "hash": "sha256:f3a2...",
    "source_token_count": 245000,
    "augmented_row_count": 18420,
    "training_scope": "voice"
  },
  "hyperparams": {
    "rank": 32,
    "alpha": 64,
    "learning_rate": 0.0001,
    "epochs": 3,
    "batch_size": 4,
    "general_data_mix_pct": 30
  }
}
```

`base_model_revision` matters — Hugging Face model snapshots change. A LoRA trained against revision A applied on revision B drifts silently. The local provider must verify this on load and refuse mismatch.

### `~/.jigga/loras/<name>/v<N>/eval.json`

Written by `lora.evaluate` (or by `lora.train` as a final step).

```json
{
  "schema_version": 1,
  "adapter": "voice/v3",
  "baseline": "voice/v2",
  "evaluated_at": "2026-05-30T13:05:11Z",
  "facts_recall": {
    "total": 60,
    "correct": 51,
    "score": 0.85,
    "examples": [
      {"question": "How do I size PRs?", "expected": "narrow, <400 lines", "got": "narrow, <400 lines", "match": true}
    ]
  },
  "general_capability": {
    "harness": "mmlu_subset_v1",
    "adapter_score": 0.612,
    "baseline_score": 0.624,
    "delta": -0.012,
    "delta_pct": -1.92
  },
  "regression": {
    "threshold_pct": -5.0,
    "verdict": "passed"
  }
}
```

When there is no previous version, `baseline` is `null` and the baseline score comes from the base model with no adapter attached.

### Activation gate logic

```python
def can_activate(eval_payload: dict, threshold_pct: float = -5.0,
                 accept_regression: bool = False) -> tuple[bool, str | None]:
    cap = eval_payload["general_capability"]
    if cap["baseline_score"] == 0:
        return True, None  # nothing to compare
    delta_pct = (cap["delta"] / cap["baseline_score"]) * 100
    if delta_pct < threshold_pct and not accept_regression:
        return False, (
            f"capability regression {delta_pct:.1f}% exceeds threshold "
            f"{threshold_pct}%. Use --accept-regression to override."
        )
    return True, None
```

The threshold is a one-line policy. Pull it from `~/.jigga/config.yaml` at `models.loras.regression_threshold_pct` — default `-5.0`, configurable per user. Negative numbers because a regression is a negative delta; positive deltas (the adapter is *better* than the base on general capability) never trigger the gate.

---

## Open decisions before v1

These are sequenced so the next agent does not paint themselves into a corner.

1. **Local-only or offload-from-day-one?** Local-first ethos says GPU-only in v1; practical answer is that most user laptops cannot train an 8B LoRA. Lean: local default + Modal/Together provider behind an explicit `network: allow` permission and a cost cap. The `OptionalCapability` setup function should detect GPU presence and surface the choice during install.

2. **Training scope schema: mirror `memory_scope` or new primitive?** Mirroring keeps the mental model unified. Lean: same shape, new file (`~/.jigga/loras/training_scopes.yaml`), separate loader. Do not reuse the `memory_scopes.yaml` file — training scopes need different sensitivity defaults (training pulls from raw far more aggressively than inference does).

3. **One global LoRA or per-domain?** "Voice LoRA" vs "voice + content_team LoRA + research_team LoRA." Lean: start global, split later if eval surfaces interference between domains. The per-domain approach can be added without breaking the profile shape — `profile: content_voice` and `profile: default_voice` are two profiles, two LoRAs.

4. **Where do evals live?** `~/.jigga/loras/<name>/v<N>/eval.json` with a stable schema. The activation flow refuses to mutate `config.yaml` when general-capability regression exceeds a configurable threshold (suggest 5%) without explicit `--accept-regression`. This is the catastrophic-forgetting guard.

5. **What base models are first-class?** The trainer needs to handle Llama 3.1 8B as the v1 target (best price/performance for personal scale, runs on a single 24GB GPU via Unsloth). Mistral/Qwen as fast-follows. Anything Anthropic/OpenAI/proprietary is out of scope — those models do not expose weights and cannot accept LoRAs from third parties.

6. **Augmentation provider.** The corpus augmenter is itself a model call. Lean: route through the existing `model_router` so it inherits the dry-run default and the user's configured providers. Do not bake in a hardcoded OpenAI dependency.

---

## What this branch ships

| File | Status |
|---|---|
| `docs/PERSONAL_LORA_RUNTIME_NOTES.md` (this file) | drafted |
| `jigga/optional_capabilities/personal_lora/__init__.py` | setup function, creates dirs + seeds example training_scopes.yaml |
| `jigga/optional_capabilities/personal_lora/manifest.yaml` | declares the four actions, `risk_level: high`, native type |
| `jigga/runtime/personal_lora.py` | handler module; `lora.list` real, other actions stub-return planned payloads |

No tests. No training. No model loading. No `jigga lora` CLI surface yet — see the next section for the target shape. The intent is to make the *shape* exist so the next agent can write the real trainer against a clear contract.

### CLI surface (target)

The four capability actions are reachable today through workflow steps, but a `jigga lora` subcommand is the right ergonomic shape for v1. Sketch:

```
jigga lora list                                    # list adapters on disk
jigga lora inspect <name>/<version>                # show adapter.json + eval.json
jigga lora train <scope> [--base <model>]          # kick off training
jigga lora evaluate <name>/<version>               # run eval against baseline
jigga lora activate <name>/<version>               # plan-apply; mutates config.yaml
    [--accept-regression] [--accept-leak]          # explicit overrides
jigga lora rollback <profile>                      # revert profile.lora to previous
jigga lora deactivate <profile>                    # set profile.lora to null
jigga lora training-scopes [list|inspect]          # browse training_scopes.yaml
```

`activate` runs through the existing plan/apply flow — it writes to `config.yaml`, which is a state file the user reviews via `jigga plan` before `jigga apply --approve`. The two `--accept-*` flags map to the regression and privacy gates above; both emit sticky audit events.

`rollback` and `deactivate` are config-only — they do not touch adapter files. Disk artifacts are immutable: a deleted profile reference does not delete the adapter, just stops pointing at it. Adapter deletion is a separate `jigga lora rm <name>/<version>` (not in v1 sketch — explicit `rm -rf` on the directory works fine for now).

### Deliberately not wired yet

Two one-line changes are intentionally left out of this branch:

| Wire | Where | Why deferred |
|---|---|---|
| `personal-lora` entry in `REGISTRY` | `jigga/optional_capabilities/__init__.py` | Adding it makes the capability appear in `jigga capabilities install`. A user who installs a high-risk capability that returns `status: "planned"` from train/evaluate/activate would reasonably be confused. The next PR adds this entry alongside the first real action. |
| `runtime.personal_lora` entry in `HANDLERS` | `jigga/runtime/dispatcher.py` | Same logic — registering the handler means `lora.*` actions resolve at workflow-step time. Until the actions do real work, leaving them unresolved is more honest than surfacing stubs. |

The handler module and manifest both exist on disk — a developer working on the implementation can import the handler directly and add it locally to verify shapes without the broader runtime treating it as a shipped feature. The first commit of the implementation PR should be: flip both switches above + add the `lora.list` test described in step 1 below.

---

## Next agent picking this up

Recommended order, smallest unit of forward motion first:

1. **Land a real `lora.list` test** that exercises the directory walk against a fixture. Ten lines, anchors the test pattern for everything else.
2. **Wire a local provider** in `model_router.py`. New `kind: local_transformers` that loads a base model + optional LoRA adapter via `peft` and runs inference in-process. The provider config shape and profile `lora:` field already exist in the design doc above. Start with a single-model lazy-load; cache the loaded base across requests.
3. **Build the curation step** of `lora.train`. Read `~/.jigga/loras/training_scopes.yaml`, resolve to a corpus file, emit `lora.corpus.curated` audit event. No training yet — just the corpus assembly. This is the part where the sensitivity guard matters most; review it as a security boundary not a feature.
4. **Augmentation via `model_router`.** Routes through existing providers, dry-run default, no new external dependency.
5. **Sandboxed trainer.** This is where Unsloth/Axolotl enters via a `SandboxSpec`. Wrap the trainer CLI; the runtime never imports `unsloth` directly. The training subprocess is the only place that needs heavy ML deps.
6. **Auto-eval + activation gate.** The catastrophic-forgetting check is what makes this safe to ship.

The recommended **next concrete PR** after this scaffolding is item 2 (local provider) — it unlocks the rest. A LoRA on disk is useless until something can apply it at inference.

## Base-model upgrade flow

When a new base lands (Llama 3.1 → 3.2, or a switch from Llama to Qwen), the user-facing path is **retrain on the new base, then activate**. LoRAs do not transfer across base models — a Llama 3.1 adapter on Llama 3.2 weights produces garbage. The local provider hard-fails on mismatch (see `adapter.json` `base_model_revision` field) rather than silently degrading.

Expected sequence:

```
$ jigga lora list
voice/v3 (base: Llama-3.1-8B-Instruct)    — active in profile:voice
voice/v4 (base: Llama-3.1-8B-Instruct)    — trained, not active

# Configure the new base in models.providers (config.yaml edit).
# Then train against the new base:

$ jigga lora train voice --base meta-llama/Llama-3.2-8B-Instruct
# trains voice/v5 against the new base
# eval runs against voice/v3 — they are not directly comparable
# (different bases produce different general-capability scores),
# so the eval is INFORMATIONAL not gating. The regression threshold
# does not apply across base-model boundaries.

$ jigga lora activate voice/v5
# updates config.yaml: profiles.voice.lora -> voice_v5
# also requires profiles.voice.primary to point at a provider whose
# base_model matches voice/v5's adapter.json. Refuses on mismatch.
```

**The "blue-green" pattern.** During the upgrade window, the user can keep v3 active while v5 is training and evaluating. Switch profiles atomically when satisfied. Old versions stay on disk for fast rollback. Adapter directories are small (~50–200MB) — keep at least the last two versions per scope by default.

**What does NOT work:** quantization changes (`bf16` → `int4`) of the *base* the LoRA was trained against. The adapter sees subtly different activations. Treat this as a base change and retrain. The local provider should refuse a quantization mismatch the same way it refuses a base mismatch.

## Risk register

- **Training data leakage at inference.** LoRAs can regurgitate training data verbatim under adversarial prompts. The activation gate should ban activation against profiles used by agents whose `memory_scope` includes anything the LoRA's `training_scope` excluded. Otherwise a less-scoped agent gets implicit access to data through the weights.
- **Adapter drift across base-model versions.** A LoRA trained against Llama 3.1 8B is not safe to apply to 3.2. The `loras.<name>.base_model` field is load-bearing; the local provider must hard-fail on mismatch, not silently degrade.
- **Confabulation under low evidence.** A fact mentioned once in the corpus will be encoded weakly and the model will fill in plausible-but-wrong details with confidence. The corpus augmenter's job is to push every fact past the redundancy floor; otherwise users will see the model "remember" things that never happened.
- **GPU presence assumption.** Setup should detect missing GPU and refuse install rather than land in a half-configured state. The offload provider option becomes the escape hatch.

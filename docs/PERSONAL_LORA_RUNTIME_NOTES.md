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

No tests. No training. No model loading. No CLI surface. The intent is to make the *shape* exist so the next agent can write the real trainer against a clear contract.

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

## Risk register

- **Training data leakage at inference.** LoRAs can regurgitate training data verbatim under adversarial prompts. The activation gate should ban activation against profiles used by agents whose `memory_scope` includes anything the LoRA's `training_scope` excluded. Otherwise a less-scoped agent gets implicit access to data through the weights.
- **Adapter drift across base-model versions.** A LoRA trained against Llama 3.1 8B is not safe to apply to 3.2. The `loras.<name>.base_model` field is load-bearing; the local provider must hard-fail on mismatch, not silently degrade.
- **Confabulation under low evidence.** A fact mentioned once in the corpus will be encoded weakly and the model will fill in plausible-but-wrong details with confidence. The corpus augmenter's job is to push every fact past the redundancy floor; otherwise users will see the model "remember" things that never happened.
- **GPU presence assumption.** Setup should detect missing GPU and refuse install rather than land in a half-configured state. The offload provider option becomes the escape hatch.

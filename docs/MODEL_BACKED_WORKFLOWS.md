# Model-backed workflow steps (`draft_with_model`)

A workflow step can now *think* — route its brief through the executing agent's model — instead of only running a deterministic handler. This turns a team into a **declarative workflow**: lead → copywriter → editor, each a real model call, wired together by named outputs, and the whole run is one trace.

## The capability

Bundled native capability **`text-generation`**, action **`draft_with_model`**, handler `runtime.draft_with_model`. Unlike `content-drafting` (dry-run stubs) it makes a real `call_model` on the step's agent — so with the `chatgpt_oauth` provider it runs on your ChatGPT subscription.

- **System prompt** = the agent's `name` + `role`.
- **Brief** = the step input. A bare string is the brief as-is; a dict's `prompt`/`brief`/`instructions` is the ask and every other key is appended as a labelled `## context` section.
- **Returns the model's text directly** (not a dict), so it chains: a `.md`/`.txt` `output:` writes the prose verbatim, and a downstream `input: {x: prior_step.md}` receives that prose.
- No special permission (a model call isn't resource-gated, same as skill packs); the agent just needs a model configured. Fails loudly if the model call errors.

## Example: a team as a workflow

```yaml
id: team_launch
steps:
  - id: core_message
    agent: marketing_lead
    action: draft_with_model
    input: {prompt: "Distill <product> into a core launch message + sharpest angle."}
    output: core_message.md
  - id: copy
    agent: copywriter
    action: draft_with_model
    input:
      prompt: "Write a launch tweet and a 3-sentence LinkedIn post."
      core_message: core_message.md      # ← prior step's prose, wired in as context
    output: copy.md
  - id: review
    agent: seo_editor
    action: draft_with_model
    input: {prompt: "3 bullet notes: clarity, claims, keywords.", copy: copy.md}
    output: review.md
```

```bash
jigga workflow run team_launch
jigga trace <trace_id>     # the whole lead→copy→review chain, one id
jigga cost                 # per-agent token usage (on a subscription: $0 marginal)
```

Each step is still **one agent doing one job** — `draft_with_model` is structured handoff (output→input), not a shared conversation. To talk to a single agent, assign it a task directly; collaboration is opt-in wiring.

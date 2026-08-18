# Workflows & Workflow Inference — usage guide

This is the hands-on guide and the **source of truth for current behavior**: how
to author a workflow, run/plan/schedule it, gate risky steps for approval, and —
the differentiator — let JIGGA **propose workflows from your repeated work**.
Everything here is built and tested. For design rationale (and a few planned
extensions) see [`WORKFLOWS.md`](WORKFLOWS.md); for steps that call a model see
[`MODEL_BACKED_WORKFLOWS.md`](MODEL_BACKED_WORKFLOWS.md).

A **workflow** is a declarative playbook/SOP — a file under
`~/.jigga/workflows/<id>.yaml` — that agents, teams, schedules, or you can invoke
for repeatable work. It is *not* a mandatory central engine; it's a reusable
procedure, planned and permission-gated like the rest of JIGGA (agents-as-code).

---

## 1. The lifecycle (four verbs)

| Verb | Command | What it does |
|---|---|---|
| **Review** | `jigga workflow plan <id>` | Shows each step, its capability, and a per-step policy status (`allow` / `needs_approval` / `blocked`) **before** anything runs. |
| **Invoke** | `jigga workflow run <id>` | Executes it now. (Or the supervisor fires it on its `trigger.schedule`.) |
| **Propose** | `jigga workflow suggest` | JIGGA reads your audit log and **suggests workflows from recurring patterns**. |
| **Apply** | `jigga workflow apply <suggestion-id> --approve` | Materializes a suggestion into a real workflow file. |

A workflow is a file, so it is also readable and writable in place:
`jigga workflow cat <id>` prints its yaml and `jigga workflow save <id>` writes
it back. A save is validated first — the same checks `jigga validate` runs — and
rejected rather than written, because the supervisor picks a workflow up on its
next tick and a broken file is a broken runtime.

---

## 2. Authoring a workflow

```yaml
id: morning_day_summary
name: Morning Day Summary
purpose: Summarize the user's day each weekday morning.
status: approved                 # draft | approved (your gate; suggestions land for review)
trigger:
  schedule: "weekday 7:30am"      # friendly schedule the supervisor understands
steps:
  - id: read_calendar
    agent: daily_briefing_agent   # who runs the step (its model/permissions/memory apply)
    action: calendar.list_events  # a registered capability action
    input: {range: today}
    output: calendar_events       # a named handle later steps can reference
    approval: not_required
  - id: summarize
    agent: daily_briefing_agent
    action: summarize_day
    input: {calendar: "${calendar_events}"}   # consumes the earlier step's output
    output: summary
  - id: notify
    action: notifications.send
    input: {body: "${summary}"}
    approval: not_required
memory:
  write_summary: true             # persist the run's result to memory
permissions:
  required: [notifications.send]  # declared up front; planned against each agent's policy
```

**Step fields:** `id`, `action` (the capability), `agent` (optional — its model,
permissions, and memory scope govern the step), `input` (dict), `output` (a name
later steps can reference), `approval` (`not_required` / `required`), `optional`
(skip instead of fail if the agent is missing), `on_fail`.

### Referencing another step's output

Write `${name}` to consume a named output. **If nothing produced that name, the
step fails** and the run records `workflow.reference.unresolved` naming the
reference and what was available:

```yaml
input: {calendar: "${calendar_events}"}     # explicit — fails loudly if unresolved
```

A **bare** name that happens to match an output still resolves, so workflows
written before this syntax keep running — but each one is recorded as
`workflow.reference.implicit`, and the form is deprecated. The reason is the
asymmetry: a bare name matching *nothing* stays a literal string, which is
indistinguishable from a value you meant to write. On the precursor stack that
ambiguity let an unsubstituted guard render as its own template text, fail a
truthiness check, and publish 20 unapproved items
([`FIELD_LESSONS_HMX_PRODUCTION.md`](FIELD_LESSONS_HMX_PRODUCTION.md) §3.2c).
With `${}` the same mistake stops the run.

Matching is anchored — a value is a reference or it isn't. `"see ${draft} above"`
is a literal, so there is no partially-substituted state to reason about.

To find what still needs migrating: `jigga audit --type workflow.reference.implicit`.

### Model steps that feed other steps must declare their shape

A step can be **model-backed** (`action: draft_with_model`, or a v2 `type: llm`
node) so it actually *thinks* — see
[`MODEL_BACKED_WORKFLOWS.md`](MODEL_BACKED_WORKFLOWS.md).

An untyped model step returns whatever the model produced. That's fine for the
**last** step in a chain, whose output a human reads. It is not fine when another
step consumes it, so **`jigga plan` blocks that**:

```
✗ draft   model step 'draft' declares no output_fields but its output is
          consumed by save. Add output_fields, or stop referencing it — an
          untyped model reply is whatever shape the model felt like
          returning that day.
```

Declare the shape and it runs:

```yaml
- id: calendar_draft
  action: draft_with_model
  input: {prompt: "Draft next month's calendar"}
  output_fields:
    - {name: markdown, type: text, description: the calendar body}
  output: calendar.md
```

The declared fields are stated to the model as an explicit JSON contract and the
reply is **validated**. One field returns that field's value, so chaining reads
exactly as it did untyped. More than one returns the dict, and each field is also
addressable as `${calendar.md.markdown}`.

Why this is a hard block rather than a warning: on the precursor stack an untyped
node ran correctly for months on one machine and corrupted a file on another. It
returned `{"markdown_lines": [...]}` instead of prose, the save step wrote that
JSON object into the calendar file, and the file lost every `### Week N` header
the *next* week's workflow parses — surfacing a week later, in a different
workflow, as a content mismatch. The only difference between the two machines was
which model happened to reply with raw text. That class of bug passes every test
you write and fails on a model upgrade
([`FIELD_LESSONS_HMX_PRODUCTION.md`](FIELD_LESSONS_HMX_PRODUCTION.md) §3.1).

> Today workflows are **linear pipelines** (ordered steps + named outputs +
> approval gates), not a branching DAG. For branching *across agents*, use team
> **handoffs** (`routing.handoffs`); a full DAG engine is future work.

---

## 3. Plan → run

```bash
jigga workflow plan morning_day_summary     # review: per-step actions + policy status
jigga workflow run  morning_day_summary     # execute
```

`plan` is the Terraform-style "see it before you run it": every step shows its
capability and whether it's `allow`, `needs_approval`, or `blocked`. The whole
plan reports `can_run` only when every step is runnable.

**Scheduling:** a workflow with a `trigger.schedule` is fired automatically by the
**supervisor** on its heartbeat (`jigga supervisor start`) — no cron daemon to
manage. Run records land at `~/.jigga/runs/workflows/<id>/<run_id>/run.json`, and
every step emits audit events, so `jigga trace <run_id>` reconstructs the run.

---

## 4. Approval gating

A step blocks for a human when either:
- it sets `approval: required`, or
- its capability is **medium/high risk** and the running agent isn't `autonomous`.

`jigga workflow run` returns `needs_approval` and stops at the gate; approve via
the CLI or, if it came through a channel, reply `approve <code>`. So
"summarize my day" can run unattended while "publish externally" pauses for you.

---

## 5. Workflow inference — JIGGA proposes workflows (the differentiator)

You don't have to hand-author every SOP. JIGGA watches the **audit log** for work
you keep doing and proposes turning it into a reusable, schedulable workflow.

```bash
jigga workflow suggest                       # list inferred workflows
jigga workflow suggest --min-count 3         # only patterns seen >= 3 times
jigga workflow apply <suggestion-id>         # preview (needs_approval)
jigga workflow apply <suggestion-id> --approve   # write the workflow file
```

**How it infers** (`jigga/runtime/inference.py`):
- It looks at completion events (`agent.task_completed`, `workflow.completed`).
- **Signal A — session shapes:** events are grouped into sessions (a >5-min gap
  starts a new one), runs of identical events are collapsed, and recurring
  **multi-step shapes** (length ≥ 2) seen at least `min_count` times become a
  multi-step workflow suggestion.
- **Signal B — single-action repetition:** an action that recurs ≥ `min_count`
  times (not already covered by a shape) becomes a one-step suggestion.
- It also derives a **modal hour** from when the pattern usually happens and puts
  it on the suggestion as a proposed schedule.

**Apply is gated.** `apply` without `--approve` returns the suggestion for review
(it never silently activates a recurring workflow — see "Approval Rules" in
[`WORKFLOWS.md`](WORKFLOWS.md)). With `--approve` it writes
`~/.jigga/workflows/<id>.yaml`, which you can then `plan`, edit, and `run` like
any hand-authored workflow. Re-applying an existing id is a no-op
(`already_applied`).

Example: you ask for a calendar + email summary most weekday mornings →
`jigga workflow suggest` proposes a `morning_day_summary`-style workflow with a
`~7:30am` schedule → `jigga workflow apply … --approve` makes it real → the
supervisor now runs it for you.

---

### Media nodes

A `media` node produces a picture instead of prose. It desugars to the
`media.generate_image` capability, so it inherits the whole ordinary chain —
tool grant, risk gating, approval outside autonomous mode, egress policy, audit:

```yaml
- id: post_art
  type: media                      # v1 steps use `action: media.generate_image`
  agent: designer
  input: {prompt: "A barber pole against a brick wall, warm evening light"}
  output: post.png                 # written as real bytes, not JSON
```

Set the provider up with `jigga capabilities install image-generation`. The
default driver is **Gemini (nano-banana)**; an OpenAI-compatible
`/images/generations` endpoint is the alternative. Drivers live in
`IMAGE_DRIVERS` — adding one is a single entry plus a function.

> JIGGA's *text* provider cannot serve this. `chatgpt_oauth` posts to the Codex
> responses endpoint on a subscription token; image generation is a separately
> keyed provider configured under `media.image`, and each call costs money.

## 6. DAG workflows (engine v2)

Declare `nodes` + `edges` instead of `steps` and the workflow becomes a graph:
branch on `on: success|error|always` edges, park at a `human_approval` node
until you reply `approve <code>` on your channel, and every run persists per-
node state under `~/.jigga/runs/workflows/…` so it resumes across restarts —
the supervisor advances parked runs on its heartbeat. Node types: `tool`,
`llm`, `human_approval`, `writeback`. See
[`WORKFLOW_ENGINE_V2_RUNTIME_NOTES.md`](WORKFLOW_ENGINE_V2_RUNTIME_NOTES.md)
for authoring details and semantics.

## 7. Command reference

```bash
jigga workflow list                          # installed workflows (id, name, status)
jigga workflow cat  <id>                     # print its raw yaml
jigga workflow save <id> [--content ...]     # write it back (validated, audited; stdin when omitted)
jigga workflow plan <id>                     # review steps + per-step policy
jigga workflow run  <id> [--json]            # execute now
jigga workflow runs [--active] [<id>]        # v2 runs + node-state counts
jigga workflow resume <run-id>               # advance a parked v2 run now
jigga workflow artifact <run-id> <name>      # print a file the run produced
jigga workflow artifact-save <run-id> <name> [--content ...]   # replace one (stdin when omitted)
jigga workflow suggest [--min-count N]       # inferred workflow proposals
jigga workflow apply <suggestion-id> [--approve]   # materialize a proposal
```

A step's `output:` name is a real file in the run directory, and
`artifact` / `artifact-save` are how you read and correct one. Correcting is the
point of parking a run on `human_approval`: read what the model produced, fix
the two sentences that are wrong, then approve — rather than denying and
re-running the whole graph to change a headline. A `running` run refuses the
edit, because its nodes are still writing their outputs and there is nothing to
arbitrate a race between a node and a human. Every edit is audited as
`workflow.artifact.written` with the actor, so an artifact a human rewrote stays
distinguishable from what the model wrote.

Workflows are files — version them, diff them, and roll them out with
`jigga plan` / `jigga apply` alongside the rest of your agents-as-code.

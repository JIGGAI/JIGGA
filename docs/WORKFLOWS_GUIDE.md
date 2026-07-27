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
    input: {calendar: calendar_events}   # consumes the earlier step's output by name
    output: summary
  - id: notify
    action: notifications.send
    input: {body: summary}
    approval: not_required
memory:
  write_summary: true             # persist the run's result to memory
permissions:
  required: [notifications.send]  # declared up front; planned against each agent's policy
```

**Step fields:** `id`, `action` (the capability), `agent` (optional — its model,
permissions, and memory scope govern the step), `input` (dict; string values that
match a prior step's `output` are **substituted** with that output), `output` (a
name later steps can reference), `approval` (`not_required` / `required`),
`optional` (skip instead of fail if the agent is missing), `on_fail`.

**Chaining:** steps run in order; outputs flow by **named reference**
(`output: calendar_events` → another step's `input: {calendar: calendar_events}`).
A step can be **model-backed** (`action: draft_with_model`) so it actually
*thinks* — see [`MODEL_BACKED_WORKFLOWS.md`](MODEL_BACKED_WORKFLOWS.md).

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
jigga workflow plan <id>                     # review steps + per-step policy
jigga workflow run  <id> [--json]            # execute now
jigga workflow runs [--active] [<id>]        # v2 runs + node-state counts
jigga workflow resume <run-id>               # advance a parked v2 run now
jigga workflow suggest [--min-count N]       # inferred workflow proposals
jigga workflow apply <suggestion-id> [--approve]   # materialize a proposal
```

Workflows are files — version them, diff them, and roll them out with
`jigga plan` / `jigga apply` alongside the rest of your agents-as-code.

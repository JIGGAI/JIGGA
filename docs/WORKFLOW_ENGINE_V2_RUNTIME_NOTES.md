# Workflow Engine v2 — Runtime Notes (2026-07-27)

Closes the last ClawRecipes→core parity gap (issue #140): workflows as a
**DAG with persisted, resumable run state**, instead of a one-shot linear
pipeline. Implemented in `jigga/runtime/workflow_engine.py`; the v1 linear
runner in `runtime/workflow.py` is untouched and still handles `steps:`
workflows — `run_workflow` routes on which form the yaml declares.

## Authoring

A v2 workflow declares `nodes` + `edges` instead of `steps`:

```yaml
id: publish_post
name: Draft, approve, publish
status: active
nodes:
  - id: draft
    type: llm                    # sugar for draft_with_model
    agent: content_strategist
    input: {prompt: "Draft the weekly post"}
    output: draft.md
  - id: gate
    type: human_approval
    input: {prompt: "Post looks good?"}
  - id: publish
    agent: content_strategist    # type defaults to tool
    action: notifications.send
    input: {message: draft}      # named-output chaining, same as v1
  - id: save_draft
    type: writeback              # copy an output into the shared workspace
    input: {source: draft, path: workspaces/content/last-draft.md}
edges:
  - {from: draft, to: gate}
  - {from: gate, to: publish, on: success}
  - {from: gate, to: save_draft, on: error}   # denied → keep the draft around
```

Node types: `tool` (a capability action — same dispatch, policy gate, and
input/output chaining as a v1 step), `llm` (`draft_with_model`), 
`human_approval` (parks the run), `writeback` (agent-less; write a named
upstream output to a file **under `~/.jigga/workspaces/` only** — path escapes
are rejected; it's a coordination convenience, not a filesystem capability).
Edge `on` is `success` (default), `error`, or `always`.

Semantics worth knowing:
- A node runs when all its incoming edges are resolved and at least one fired;
  a branch whose condition didn't hold is `skipped` (and skips propagate).
- A `failed` node with a fired `error`/`always` edge (or `optional: true`) is a
  **handled** failure; an unhandled failure fails the run. This is the real
  `on_fail` mechanism — route the failure to a recovery node.
- `validate_graph` rejects duplicate/unknown node refs, bad `on` values, and
  cycles; surfaced through `jigga validate` and checked again at run start.

## Runs are files, advanced on the heartbeat

Run state lives at `~/.jigga/runs/workflows/<workflow_id>/<run_id>/run.json` —
per-node status (`pending / done / failed / skipped / awaiting_approval`),
outputs, trace id, timestamps — persisted after **every** node transition, so a
run survives restarts by construction. Nothing is held in memory between
advances.

`jigga workflow run <id>` starts a run and advances it synchronously as far as
it can (an approval-free DAG completes in one call, like v1). A parked or
budget-bounded run is picked up by the **supervisor tick** (`advance_all_runs`:
oldest-first, bounded runs/tick and nodes/run, per-run trace inheritance, one
run's fault contained so the rest still advance). CLI:

```bash
jigga workflow runs [--active] [<workflow_id>]   # list runs + node-state counts
jigga workflow resume <run_id>                   # advance one run right now
```

## Approvals ride the existing queue + channels

A `human_approval` node — and any medium/high-risk tool node under a
non-autonomous mode (same risk gate as v1/agent tool calls) — parks the run:
`request_approval` into the shared queue keyed `(task_id="wfrun:<run_id>",
action="workflow_node:<workflow>:<node>")`, ask sent to the **owner
conversation** on the default channel. `approve <code>` / `deny <code>` resolve
through the normal channel gateway or `jigga approvals approve <code>`; the
next advance consumes the resolution exactly once (`consume_if_approved` /
new `consume_if_denied`). Approve → the node runs (or completes, for
`human_approval`); deny → the node fails, which error edges may handle.
Unlike v1 plans, `needs_approval` does **not** make a v2 plan unrunnable —
the run parks at that node instead of refusing to start.

### Deliverability is resolved before the run parks

An approval nobody can receive is indistinguishable from one nobody has
answered yet, and that ambiguity is how the precursor stack parked a run for 36
days — silently taking out every downstream run for a month (see
`docs/FIELD_LESSONS_HMX_PRODUCTION.md` §3.2a). So the delivery target is
resolved *before* `request_approval`, and the node records the outcome:

- `delivery: "delivered"` — the ask reached the owner conversation.
- `delivery: "undelivered"` + `delivery_error: <reason>` — no owner
  conversation on any enabled channel, no registered adapter for it, or the
  send itself raised. The run **still parks** (`jigga approve <code>` always
  works locally), but it parks visibly: `workflow.approval_undeliverable` is
  logged at `status: error`, and `jigga workflow runs` flags the node.

`parked_at` is stamped on every parked node, and `alarm_stale_approvals` — run
from the supervisor heartbeat after advancement, so an approval that arrived
this tick is never reported as unanswered — alarms once on any node parked past
`approvals.max_parked_hours` (default 24). The alarm goes out as a **desktop
notification, not through the channel**: an alarm must not depend on the
subsystem it monitors, and an undelivered ask is the likeliest reason for the
silence in the first place. `stale_alarm_at` makes the sweep idempotent, and
run status is never changed — a parked run is still legitimately parked.

## Follow-up work

- **Media nodes** (image/video/audio drivers) — the deliberate remainder of
  the parity gap; land as capabilities so they inherit policy + approvals.
- Event triggers (`calendar_event_upcoming` + offsets) for both engines.
- A Workflows-page graph view in jiggaview (nodes/edges + live run state from
  `jigga workflow runs --json`).
- v1 `on_fail` on linear steps stays unenforced; recipes should migrate to v2
  error edges when they need it.

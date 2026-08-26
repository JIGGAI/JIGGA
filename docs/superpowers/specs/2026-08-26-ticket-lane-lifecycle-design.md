# Ticket lane lifecycle

**Status:** approved design, not yet implemented
**Date:** 2026-08-26

## Problem

A ticket is marked `completed` the moment its agent's run ends. That is not
what completion means on a board, and it produces three visible faults.

**Completion is a lie.** On 2026-08-25 two tickets read `completed` having
produced nothing at all. PR #231 fixed the worst case (a run that halted on its
iteration ceiling still reported success), but the underlying rule is
unchanged: a finished *run* still ends a *ticket*. A ticket the dev has merely
handed to QA is not done, and the board cannot tell the difference.

**The board fragments.** `fire_handoffs` creates a new task per hop, so one
piece of work becomes several rows. The end-to-end run on 2026-08-26 produced
four tickets for one request, including `task_5a5194598d8a "Handoff from
engineering-team-dev: ready_for..."`. Nobody asked for those, and the original
ticket stops reflecting the work.

**Lanes are decorative.** `engineering-team` declares backlog → in-progress →
testing → ready-for-pr → done. In the same run, every ticket stayed in the lane
it was created in. `Task.lane` is documented as "orthogonal to state", and in
practice nothing moved it.

There is also no record on a ticket of what was done. An agent's closing
summary is appended to `shared-context/agent-outputs/<agent-id>.md`, which is
per-agent, not per-ticket — reconstructing one ticket's history means reading
every agent's file and correlating by timestamp.

## Approach

Make the lane the ticket's lifecycle and let one ticket travel it.

1. **Lane is the truth.** `completed` is reachable only from the `done` lane.
2. **One ticket travels.** Handing work on reassigns the existing ticket
   instead of creating a new one.
3. **The runtime moves the lane**, derived from the handoff, so an agent cannot
   leave the board disagreeing with who holds the work.
4. **Every run records a comment** on the ticket it worked.

Non-team tasks — anything with no lane — keep today's behaviour exactly. This
is a team-board feature, not a change to the task queue.

## Design

### State versus lane

`state` continues to describe the *run*: `pending` (waiting for its assignee),
`claimed`, `running`, `failed`, `blocked`. `lane` describes where the *work*
sits. The one coupling: **only a move into `done` may set `completed`**, and it
is the sole way a ticket becomes complete.

At the end of a successful run on a lane-managed ticket the runtime no longer
writes `completed`. It writes what the run actually established:

| what the agent did | assignee | lane | state |
|---|---|---|---|
| handed off | the new agent | derived (below) | `pending` |
| closed it (lead only) | unchanged | `done` | `completed` |
| neither | team lead | `backlog` | `pending` |
| run failed | unchanged | unchanged | `failed` |

### Lane derivation

Deriving from the target role alone does not work here, because the lead owns
three lanes: `backlog` (work bounced back), `ready-for-pr` (QA passed), and
`done`. The transition is unambiguous where the destination is not:

| handoff | lane |
|---|---|
| lead → dev | `in-progress` |
| dev → test | `testing` |
| test → dev | `in-progress` (QA rejected — back to the author) |
| test → lead | `ready-for-pr` (QA passed) |
| any → lead, unhandled | `backlog` |

QA rejection is a first-class transition, not a bounce. Without it a rejected
ticket would fall through to the bounce lane and lose the fact that it was
actively sent back, which is exactly the kind of silent flattening the rest of
this design exists to stop.

Declared per team so it is readable and overridable:

```yaml
lane_transitions:
  rules:
    - {from: lead, to: dev,  lane: in-progress}
    - {from: dev,  to: test, lane: testing}
    - {from: test, to: dev,  lane: in-progress}    # QA rejected
    - {from: test, to: lead, lane: ready-for-pr}   # QA passed
  bounce_lane: backlog
```

A transition with no rule leaves the lane unchanged and emits
`ticket.lane.underived` — the same principle as #233, where silently dropping
input is what made the loss invisible.

`done` is deliberately not assignment-driven. It is the lead closing the ticket
after confirming the merge.

### New actions

```
tickets.handoff(ticket_id, assignee, comment)   # reassign + lane + state + comment, atomically
tickets.comment(ticket_id, text)                # a note, any time
tickets.close(ticket_id, comment)               # lead only: lane=done, state=completed
```

All three declare `action_inputs`, so the model is told the field names rather
than guessing them — the failure #233 fixed for `task.assign`. `task.assign`
keeps its current meaning: create genuinely new work.

`tickets.close` is refused for a ticket not in `ready-for-pr`, and refused for
an agent that is not the team lead. Both refusals are audited.

This assumes every ticket reaches `done` through `ready-for-pr`. That holds for
`engineering-team`, whose lanes describe a code-review pipeline. A team whose
board has no such lane would need its own terminal rule; out of scope here, and
the refusal is loud rather than silent if it happens.

### Comments

`Task.comments: list[{author, at, text}]`, written two ways:

- **Automatic.** The agent's closing summary is appended at the end of every
  run on a lane-managed ticket. It is the text already being written to
  `agent-outputs`, so this costs nothing and cannot be forgotten.
- **Explicit.** `tickets.comment` for richer or mid-run notes. Role
  instructions ask for what changed, how to verify it, and what is left.

A comment failure is logged and never fails the run.

### Bounce guard

Bouncing unhandled work to the lead risks a dev↔lead loop. `metadata.bounces`
increments on each bounce; at **3** the ticket becomes `blocked` with a comment
explaining why, and the supervisor stops waking anyone for it. Bounded, visible,
and it stops rather than burning tokens.

### Retiring handoff tickets

`fire_handoffs` returns early for a team whose ticket is lane-managed. The
mechanism stays for non-lane teams; it is the ticket-spawning that stops.

## Files

| file | change |
|---|---|
| `jigga/core/models.py` | `Task.comments` |
| `jigga/runtime/tasks.py` | comment append; lane-managed predicate |
| `jigga/runtime/lanes.py` | transition table, derivation, validation |
| `jigga/runtime/agent.py` | end-of-run derivation; auto-comment |
| `jigga/runtime/handlers.py` | `tickets.handoff` / `.comment` / `.close` |
| `jigga/runtime/handoffs.py` | early return for lane-managed teams |
| `jigga/runtime/capabilities.py` | declare the three actions + `action_inputs` |
| `~/.jigga/teams/engineering-team.yaml` + recipes | `lane_transitions`, role instructions |

## Testing

Unit: only `done` yields `completed`; each transition derives its lane; an
underived transition leaves the lane and emits the event; handoff moves
assignee+lane+state together; the bounce guard trips at exactly 3; auto-comment
lands on every run; `tickets.close` refuses a non-lead and a ticket outside
`ready-for-pr`; non-team tasks are untouched.

Integration: one ticket walks backlog → in-progress → testing → ready-for-pr →
done, carrying a comment from each agent, and **no second ticket is created**.

Live: re-run the 2026-08-26 end-to-end and assert the board shows one row, not
four.

## Consequences

**`completed` means less, and that is the point.** The board reads 81 completed
today; after this only work that reached `done` qualifies. Same direction as
#231, larger effect. Existing tickets are not migrated — the rules apply to
tickets worked after the change.

**Tickets can now stall in `ready-for-pr`.** Nothing merges automatically, so a
ticket sits there until the lead confirms the merge and closes it. That is
honest — the work genuinely is not done — but it is a queue someone has to
watch, and it did not exist before.

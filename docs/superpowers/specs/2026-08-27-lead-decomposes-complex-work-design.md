# The lead decomposes complex work

**Status:** approved design, not yet implemented
**Date:** 2026-08-27

## Problem

A complex ask arrives as one ticket and the lead has no way to break it up.
Its only tools for moving work are `tickets.handoff`, which passes the whole
thing to one agent, and `task.assign`, which creates an unrelated ticket and is
now refused when the lead is holding a lane-managed ticket — precisely this
case. So "build a new website" is handed to one dev as a single ticket, or it
is not handed on at all.

Nothing links tickets to each other. `Task` carries `id`, `title`, `assignee`,
`lane`, `state` and a free `metadata` dict; there is no parent, no children, no
way to ask whether the overall ask is finished.

## Approach

A dedicated verb for a distinct act.

```
tickets.decompose(ticket, summary, plan, stories=[{title, description, assignee}, ...])
```

The alternative was a carve-out in the `task.assign` refusal when a `parent`
field is set. That was rejected: it reopens the judgment the refusal exists to
remove, and it asks the model to decide "handoff or decomposition?" at the exact
moment it is already reaching for the wrong tool. A separate verb makes the
intent unambiguous and leaves the refusal untouched.

One call does the whole thing: create the stories, link them to the parent, put
the parent to sleep, and rewrite the parent so it reads as a status page.

## Design

### The epic reads on its own

The epic's description is rewritten to carry the plan's summary, the path to the
full plan, and the story list. A person opening the ticket sees the shape of the
work without opening anything else; a person wanting the detail has the path.

```
## Plan
Static Next.js app, deployed from CI. Cut by surface: scaffold first so the
other two can proceed in parallel, nav before deploy so there is something to
smoke-test.

Full plan: shared-context/plans/new-website.md

## Stories
- task_B  Scaffold the Next.js app   -> engineering-team-dev
- task_C  Build the nav              -> engineering-team-dev
- task_D  Deploy pipeline            -> engineering-team-devops

## Original request
<the ask, preserved verbatim>
```

The summary is required. A path alone would make the board unreadable without a
second lookup, and the file is not injected into anyone's context.

### Parent and children

In `metadata`, not the schema: each story gets `parent: <epic id>`; the epic
gets `children: [ids]` and `plan: <path>`. `metadata` is already a free dict and
already carries `team_id`, `bounces` and `context`.

### Waiting

The epic stays in the lane it was already in and takes a new task state,
`waiting`, meaning "waiting on children". Lane says where the work is; state
says whether anyone is acting on it — the same split the rest of the board uses. It costs almost
nothing: `tasks_for_agent` already selects only `pending`, so the supervisor
skips a waiting epic for free — no wake, no bounce, no tokens. `blocked` was
rejected for this: it means "bounced too often, a human must look", and
overloading it would make a healthy epic indistinguishable from a stuck ticket.

### Release

When a story reaches `completed`, the runtime checks its parent. When every
child is complete, the epic moves to the team's **close lane** (`ready-for-pr`)
as `pending` to the lead, audited as `ticket.children_complete`.

The close lane specifically, not wherever it was waiting: `tickets.close`
refuses any ticket outside that lane, so an epic released into `in-progress`
could never be closed at all. Releasing it to the close lane means the existing
close path works unchanged and the lead's final act is the same one it performs
on every other ticket.

**A failed or blocked child releases the epic immediately**, with the reason,
rather than leaving it asleep. One dead story would otherwise park the epic
forever — the silent stall this whole line of work exists to remove.

### Stories start in the backlog

`decompose` creates each story `pending` in the board's first lane (`backlog`
for both dev teams), assigned to the named agent. It does not also perform handoffs. Decomposition and movement
are different acts, and an atomic call that also moved four tickets would be
much harder to reason about when one of them fails.

### Guardrails

- Lead only. Decomposition is a lead act; refused for anyone else, audited.
- `description` required on every story, the same discipline `task.assign` has —
  a story without a brief is the bug that produced a six-word ticket.
- At most 20 stories, so a confused lead cannot flood the board.
- Refused if the ticket already has children, so a re-run cannot duplicate them.
- Refused for a ticket that is not lane-managed.

## Files

| file | change |
|---|---|
| `jigga/core/models.py` | `waiting` added to `TaskState` |
| `jigga/runtime/decompose.py` | new: validation, story creation, epic rewrite |
| `jigga/runtime/handlers.py` | `tickets.decompose` branch |
| `jigga/runtime/capabilities.py` | the action + its `action_inputs` |
| `jigga/runtime/ticket_outcome.py` | release the parent when children finish |
| `jigga/runtime/lanes.py` | `render_lanes` explains when to decompose vs hand off |
| team configs + recipes | `tickets.decompose` granted to both leads |

## Testing

Unit: a decompose creates N linked stories and puts the epic to `waiting`; the
epic's description carries summary, path and story list; the last child
completing releases the epic to the lead; a failed child releases it immediately
with the reason; every guardrail refuses and audits (non-lead, no description,
over the cap, already decomposed, not lane-managed).

Integration: an epic with three stories walks to `done` — stories complete
through the normal board, the epic wakes exactly once at the end, and the
supervisor never wakes the lead for it while it waits.

Live: give the lead a genuinely complex ask and confirm it writes a plan,
creates stories, and that the board shows one epic plus its stories rather than
one overloaded ticket.

## Consequences

**The lead now has three verbs** — `handoff` for a ticket travelling,
`decompose` for one splitting, `close` for one finishing. That is more surface
and one more choice to get wrong. `render_lanes` will state when each applies,
which is where the board already teaches its own rules.

**`waiting` is a state nothing currently produces**, so every consumer of task
state gains a case it has never seen. The board views and any counting by state
will need to account for it.

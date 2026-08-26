# Ticket Lane Lifecycle — Task 7 Post-Deploy Runbook

**Status:** DEFERRED — do not run any command in this document until the
gating conditions below are met.

## 1. Why this is deferred

Task 7 of the ticket-lane-lifecycle plan calls for pointing the live
teams at two new capability actions, `tickets.handoff` and
`tickets.close`, by running `jigga agents set ... --recipe` against
`~/.jigga` (the live team config that the running `jigga-supervisor`
service actually reads).

The code change is not scoped to `engineering-team`. It applies to every
team whose board runs the ticket lifecycle, and there are **two** of
those on this box — see Section 1.1 before running anything.

Production does not run from this branch. It runs from `~/jigga-stable`,
which at the time this runbook was written was pinned to commit
`24ce553` and does **not** contain `tickets.handoff` or `tickets.close`.
Telling the live agents to call those actions before the
deployed runtime supports them would break those teams: the agents
would attempt to hand off or close tickets using capabilities that do not
exist in the running process, with no fallback.

Every step below is written to be run **after all three** of the
following are true, in order:

1. `feat/ticket-lane-lifecycle` has been merged to the trunk branch that
   `~/jigga-stable` tracks.
2. `~/jigga-stable` has been moved to the merged commit (fast-forwarded /
   redeployed — not just fetched).
3. `jigga-supervisor.service` has been restarted so the running process
   is actually serving the new code, not just the checked-out commit.

Do not run Section 3, 4, or 5 of this document until all three are
confirmed. Sections 1.1 and 2 are read-only and safe at any time.

## 1.1 Which teams this change affects — read before deploying

The runtime treats a team as lifecycle-managed when its board has lanes
**and** a usable transition rule **and** a bounce lane **and** a terminal
`done` lane (`jigga.runtime.lanes.is_lifecycle_managed`). Only those
teams change behaviour. Verified read-only against `~/.jigga/teams`:

| Team | Lanes | Lifecycle-managed | `routing.handoffs` | Action needed |
|---|---|---|---|---|
| `engineering-team` | backlog → in-progress → testing → ready-for-pr → done | **yes** | 4 rules | **Sections 3–5** |
| `seven-development-team` | backlog → in-progress → testing → ready-for-pr → done | **yes** | 4 rules | **Sections 3–5** |
| `marketing-team` | brief → drafting → review → published | no | 0 rules | none — unchanged |
| `social_content_team` | (no lanes) | no | 2 rules | none — unchanged |

Re-run this check at any time:

```bash
cd ~/JIGGA && source .venv/bin/activate && python -c "
from pathlib import Path
from jigga.core.config import load_teams
from jigga.runtime.lanes import close_lane, is_lifecycle_managed, team_lanes
for tid, t in sorted(load_teams(Path.home()/'.jigga'/'teams').items()):
    routing = t.routing if isinstance(t.routing, dict) else {}
    print(tid, 'lifecycle_managed=', is_lifecycle_managed(t),
          'close_lane=', close_lane(t),
          'lanes=', [l.id for l in team_lanes(t)],
          'routing.handoffs=', len(routing.get('handoffs') or []))
"
```

### ⚠️ Warning: a lifecycle-managed team loses `routing.handoffs` at deploy

`fire_handoffs` stands itself down for a lifecycle-managed team — the
whole point of the change is that one ticket travels the board instead of
a new ticket being spawned per hop. **Its `routing.handoffs` rules stop
producing anything the moment the new code is serving.** The replacement
is the `tickets.handoff` capability, and an agent that has not been
granted it has no way to pass work on at all: its board simply stops.

Both lifecycle-managed teams are in this position today. Neither
`engineering-team` nor `seven-development-team` currently grants
`tickets.handoff` or `tickets.close` to any agent, and both rely on four
`routing.handoffs` rules:

```
<team>-dev     -> <team>-test  when ready_for_qa
<team>-devops  -> <team>-test  when ready_for_qa
<team>-test    -> <team>-lead  when qa_passed
<team>-test    -> <team>-dev   when qa_failed
```

So Sections 3 and 4 are **not optional polish** — they are the migration.
Run them for *every* lifecycle-managed team, in the same maintenance
window as the deploy. Any lane-bearing team that becomes
lifecycle-managed later (by gaining a `done` lane, a bounce lane and a
transition rule) must be migrated the same way before it is deployed, or
its board stops.

Two further notes for both teams:

- **`devops` has no transition rule.** The default table covers
  `lead→dev`, `dev→test`, `test→dev` and `test→lead` only, so a handoff
  to or from `<team>-devops` leaves the ticket's lane unchanged and emits
  `ticket.lane.underived` (it is recorded, not silently dropped). Add an
  explicit `lane_transitions.rules` entry to the team YAML if devops
  should move the board.
- **`tickets.move` can no longer reach `done`.** Every
  `seven-development-team` agent currently holds `tickets.move`, which
  before this change was an ungated second door into completion. Moves
  into `done` are now refused and audited as
  `team.ticket.move.refused`; `tickets.close` (lead only, from
  `ready-for-pr`) is the only route. Expect — and ignore — a few of those
  refusals in the first days after deploy while the role instructions
  from Section 3 take hold.

## 2. Already confirmed — no `lane_transitions` config is required for either team

Step 1 of the original task-7 brief (read-only verification) has already
been run against the real `engineering-team` **and**
`seven-development-team` configs on this branch and passed. Both declare
the same five lanes, so the default transition table already resolves
every transition either team needs, with zero additional YAML (read
`<team>` below as either team id):

| From | To | Lane |
|---|---|---|
| `<team>-lead` | `<team>-dev` | `in-progress` |
| `<team>-dev` | `<team>-test` | `testing` |
| `<team>-test` | `<team>-dev` | `in-progress` (QA rejected) |
| `<team>-test` | `<team>-lead` | `ready-for-pr` (QA passed) |
| close lane (derived from the `-> lead` rule) | | `ready-for-pr` |
| bounce (unhandled ticket) | | `backlog` |

No `lane_transitions.rules` block needs to be added to
`~/.jigga/teams/engineering-team.yaml` or
`~/.jigga/teams/seven-development-team.yaml`. This finding does not need
to be re-verified before deploy, but it can be re-run at any time (it is
read-only) with:

```bash
cd ~/JIGGA && source .venv/bin/activate && python -c "
from pathlib import Path
from jigga.core.config import load_teams
from jigga.runtime.lanes import close_lane, derive_lane, lane_transitions
teams = load_teams(Path.home()/'.jigga'/'teams')
for tid in ('engineering-team', 'seven-development-team'):
    team = teams[tid]
    print(tid, 'close_lane:', close_lane(team))
    print('  transitions:', lane_transitions(team))
    for a, b in [(f'{tid}-lead', f'{tid}-dev'),
                 (f'{tid}-dev', f'{tid}-test'),
                 (f'{tid}-test', f'{tid}-lead')]:
        print(f'    {a} -> {b}: {derive_lane(team, a, b)}')
"
```

Expected output, for each team: `close_lane: ready-for-pr`, then
`in-progress`, `testing`, `ready-for-pr` for the three printed
transitions respectively. If any transition comes back `None`, that
team's role names no longer match `lead`/`dev`/`test` and an explicit
`lane_transitions.rules` block using the real role names must be added
to its YAML before proceeding with Section 3. If `close_lane` comes back
`None`, the team has no `-> lead` rule and `tickets.close` will fall back
to a lane literally named `ready-for-pr` — which, if the board has
renamed it, means nothing can be closed.

## 3. Update each role's instructions via the recipe

Run only after the gating conditions in Section 1 are met. These are
verbatim from the task-7 brief.

```bash
cd ~/JIGGA && source .venv/bin/activate
jigga agents set engineering-team-lead role "Triages incoming work into tickets and assigns them. Hand a ticket on with tickets.handoff — never create a second ticket for work that already has one. You alone close a ticket, with tickets.close, and only once it is in ready-for-pr and the PR is merged. Leave a comment saying what you decided and why." --recipe
jigga agents set engineering-team-dev role "Implements the ticket assigned to you. When the work is ready for QA, hand the SAME ticket to engineering-team-test with tickets.handoff and a comment covering what changed and how to verify it. Never create a new ticket for work you were handed." --recipe
jigga agents set engineering-team-test role "Verifies the ticket assigned to you. Hand it to engineering-team-lead with tickets.handoff when it passes, or back to engineering-team-dev when it does not, with a comment saying exactly what you ran and what you saw." --recipe
```

The same three instructions for `seven-development-team`, which is
lifecycle-managed with the identical board and therefore loses its
`routing.handoffs` in exactly the same way:

```bash
jigga agents set seven-development-team-lead role "Triages incoming work into tickets and assigns them. Hand a ticket on with tickets.handoff — never create a second ticket for work that already has one. You alone close a ticket, with tickets.close, and only once it is in ready-for-pr and the PR is merged. Leave a comment saying what you decided and why." --recipe
jigga agents set seven-development-team-dev role "Implements the ticket assigned to you. When the work is ready for QA, hand the SAME ticket to seven-development-team-test with tickets.handoff and a comment covering what changed and how to verify it. Never create a new ticket for work you were handed." --recipe
jigga agents set seven-development-team-test role "Verifies the ticket assigned to you. Hand it to seven-development-team-lead with tickets.handoff when it passes, or back to seven-development-team-dev when it does not, with a comment saying exactly what you ran and what you saw." --recipe
```

`<team>-devops` is deliberately not given a `tickets.handoff`
instruction: no transition rule covers it, so a devops handoff would
leave the lane behind (see the devops note in Section 1.1). Decide
whether devops needs a rule before instructing it to hand tickets on.

## 4. Grant the new actions to the team

Read the current `tools` list for each agent first — never guess at it,
and never replace it wholesale.

```bash
for a in engineering-team-lead engineering-team-dev engineering-team-test \
         seven-development-team-lead seven-development-team-dev seven-development-team-test; do
  jigga agents get "$a" tools
done
```

For each of the six agents, append `tickets.handoff` to its existing
`tools` list. For `engineering-team-lead` and
`seven-development-team-lead` only, also append `tickets.close`. Apply
the change with:

```bash
jigga agents set <id> tools '<json array>' --recipe
```

**Warning:** `<json array>` must be the agent's existing tools list from
the `get` output above, with the new entries appended — never a fresh
list containing only the new entries. Replacing the list instead of
extending it will silently strip every tool the agent currently has.
This matters more for `seven-development-team`, whose agents already
carry a dozen tools each (including `shell.run`, `web.fetch` and
`tickets.move`), than for `engineering-team`, whose lists are short.

Leave `tickets.move` in place where it is already granted. It is still
how a ticket is filed by hand between lanes; it simply can no longer
reach `done`.

## 5. Verify the change landed

Confirm the recipe file actually carries the new instructions:

```bash
grep -c "tickets.handoff" ~/.jigga/recipes/engineering-team.md
grep -c "tickets.handoff" ~/.jigga/recipes/seven-development-team.md
```

Expected: `3` or more from each (one mention per role recipe at
minimum). A `0` from either file means that team was not migrated and its
board will stall on the first handoff after deploy.

### Live smoke test

File one ticket for **each** migrated team and watch it travel the board.
Confirm the board shows **one row** for that ticket moving through:

```
backlog -> in-progress -> testing -> ready-for-pr -> done
```

If instead you see **four separate rows**, each independently marked
`completed`, the handoff wiring did not take — a new ticket is being
created at each stage instead of the existing one being handed off and
relabeled. Do not consider this rollout complete until the single-row
behavior is confirmed, on both teams.

If instead the ticket **stops moving** — it sits with one agent and
nothing follows — the agent was not granted `tickets.handoff` and its
old `routing.handoffs` rule is no longer firing. That is the Section 1.1
failure mode; finish Section 4 for that team.

## 6. What changes for humans

- Tickets will now sit in `ready-for-pr` until the lead explicitly closes
  them with `tickets.close` — they no longer auto-complete when QA signs
  off.
- The board's `completed` count will drop, because only work that has
  actually been closed by the lead after a merged PR counts as done —
  not merely work that passed QA.
- A ticket sitting in `ready-for-pr` no longer bounces back to `backlog`
  when the lead runs without a merge — waiting is not failing. It stays
  put, assigned to the lead, until it is closed. That queue needs a human
  watching it.
- `marketing-team` and `social_content_team` are untouched by all of the
  above. They are not lifecycle-managed, so their runs still complete
  their tasks and their `routing.handoffs` still fire.

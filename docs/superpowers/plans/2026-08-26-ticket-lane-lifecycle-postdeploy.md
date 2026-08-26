# Ticket Lane Lifecycle — Task 7 Post-Deploy Runbook

**Status:** DEFERRED — do not run any command in this document until the
gating conditions below are met.

## 1. Why this is deferred

Task 7 of the ticket-lane-lifecycle plan calls for pointing the live
`engineering-team` at two new capability actions, `tickets.handoff` and
`tickets.close`, by running `jigga agents set ... --recipe` against
`~/.jigga` (the live team config that the running `jigga-supervisor`
service actually reads).

Production does not run from this branch. It runs from `~/jigga-stable`,
which at the time this runbook was written was pinned to commit
`24ce553` and does **not** contain `tickets.handoff` or `tickets.close`.
Telling the live engineering-team agents to call those actions before the
deployed runtime supports them would break the live team: the agents
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
confirmed. Section 2 is read-only and safe at any time.

## 2. Already confirmed — no `lane_transitions` config is required for `engineering-team`

Step 1 of the original task-7 brief (read-only verification) has already
been run against the real `engineering-team` config on this branch and
passed. The default transition table already resolves every transition
the team needs, with zero additional YAML:

| From | To | Lane |
|---|---|---|
| `engineering-team-lead` | `engineering-team-dev` | `in-progress` |
| `engineering-team-dev` | `engineering-team-test` | `testing` |
| `engineering-team-test` | `engineering-team-dev` | `in-progress` (QA rejected) |
| `engineering-team-test` | `engineering-team-lead` | `ready-for-pr` (QA passed) |
| bounce (unhandled ticket) | | `backlog` |

No `lane_transitions.rules` block needs to be added to
`~/.jigga/teams/engineering-team.yaml`. This finding does not need to be
re-verified before deploy, but it can be re-run at any time (it is
read-only) with:

```bash
cd ~/JIGGA && source .venv/bin/activate && python -c "
from pathlib import Path
from jigga.core.config import load_teams
from jigga.runtime.lanes import derive_lane, lane_transitions
team = load_teams(Path.home()/'.jigga'/'teams')['engineering-team']
print('transitions:', lane_transitions(team))
for a, b in [('engineering-team-lead','engineering-team-dev'),
             ('engineering-team-dev','engineering-team-test'),
             ('engineering-team-test','engineering-team-lead')]:
    print(f'  {a} -> {b}: {derive_lane(team, a, b)}')
"
```

Expected output: `in-progress`, `testing`, `ready-for-pr` for the three
printed transitions respectively. If any comes back `None`, the live
team's role names no longer match `lead`/`dev`/`test` and an explicit
`lane_transitions.rules` block using the real role names must be added
to the team YAML before proceeding with Section 3.

## 3. Update each role's instructions via the recipe

Run only after the gating conditions in Section 1 are met. These are
verbatim from the task-7 brief.

```bash
cd ~/JIGGA && source .venv/bin/activate
jigga agents set engineering-team-lead role "Triages incoming work into tickets and assigns them. Hand a ticket on with tickets.handoff — never create a second ticket for work that already has one. You alone close a ticket, with tickets.close, and only once it is in ready-for-pr and the PR is merged. Leave a comment saying what you decided and why." --recipe
jigga agents set engineering-team-dev role "Implements the ticket assigned to you. When the work is ready for QA, hand the SAME ticket to engineering-team-test with tickets.handoff and a comment covering what changed and how to verify it. Never create a new ticket for work you were handed." --recipe
jigga agents set engineering-team-test role "Verifies the ticket assigned to you. Hand it to engineering-team-lead with tickets.handoff when it passes, or back to engineering-team-dev when it does not, with a comment saying exactly what you ran and what you saw." --recipe
```

## 4. Grant the new actions to the team

Read the current `tools` list for each agent first — never guess at it,
and never replace it wholesale.

```bash
for a in engineering-team-lead engineering-team-dev engineering-team-test; do
  jigga agents get "$a" tools
done
```

For each of the three agents, append `tickets.handoff` to its existing
`tools` list. For `engineering-team-lead` only, also append
`tickets.close`. Apply the change with:

```bash
jigga agents set <id> tools '<json array>' --recipe
```

**Warning:** `<json array>` must be the agent's existing tools list from
the `get` output above, with the new entries appended — never a fresh
list containing only the new entries. Replacing the list instead of
extending it will silently strip every tool the agent currently has.

## 5. Verify the change landed

Confirm the recipe file actually carries the new instructions:

```bash
grep -c "tickets.handoff" ~/.jigga/recipes/engineering-team.md
```

Expected: `3` or more (one mention per role recipe at minimum).

### Live smoke test

File one ticket for the engineering team and watch it travel the board.
Confirm the board shows **one row** for that ticket moving through:

```
backlog -> in-progress -> testing -> ready-for-pr -> done
```

If instead you see **four separate rows**, each independently marked
`completed`, the handoff wiring did not take — a new ticket is being
created at each stage instead of the existing one being handed off and
relabeled. Do not consider this rollout complete until the single-row
behavior is confirmed.

## 6. What changes for humans

- Tickets will now sit in `ready-for-pr` until the lead explicitly closes
  them with `tickets.close` — they no longer auto-complete when QA signs
  off.
- The board's `completed` count will drop, because only work that has
  actually been closed by the lead after a merged PR counts as done —
  not merely work that passed QA.

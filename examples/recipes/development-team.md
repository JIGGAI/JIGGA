---
id: development_team
name: Development Team
kind: team
version: 0.1.0
description: A small engineering team — lead, dev, devops, QA — working a shared ticket board with QA before PR.
purpose: Take engineering work from triage to a reviewed, QA-passed change without anyone opening a PR the tests haven't seen.
memory_scope: manager_view
routing:
  lead: lead
  # The QA gate, as files rather than prose: dev hands to QA, QA hands back to
  # the lead, and only the lead opens the PR. Each handoff writes an auditable
  # decision (`jigga team decisions development_team`).
  handoffs:
    - from: "{{teamId}}-dev"
      to: "{{teamId}}-test"
      when: ready_for_qa
    - from: "{{teamId}}-devops"
      to: "{{teamId}}-test"
      when: ready_for_qa
    - from: "{{teamId}}-test"
      to: "{{teamId}}-lead"
      when: qa_passed
    - from: "{{teamId}}-test"
      to: "{{teamId}}-dev"
      when: qa_failed
# The ticket board. `gate:` means only that role may move a ticket OUT of the
# lane, which is what makes "QA before PR" a rule the runtime enforces rather
# than a paragraph everyone is asked to remember.
lanes:
  - id: backlog
    description: Triaged, specified, and ready to pick up.
  - id: in-progress
    description: Being built. One ticket per agent at a time.
  - id: testing
    description: Built and awaiting QA verification.
    gate: test
  - id: ready-for-pr
    description: QA passed. The lead opens the PR from here.
    gate: lead
  - id: done
    description: Merged and closed.
policies:
  approvals:
    required_for:
      - open_pull_request
      - force_push
      - deploy
default_workflows:
  - ship_a_change
agents:
  - role: lead
    required: true
    agent:
      name: Dev Team Lead
      role: >-
        Triages incoming work into tickets, assigns them, keeps the board honest,
        and opens the PR once QA has passed. Does not write the change.
      description: Engineering lead for the development team.
      model: profile:default
      memory_scope: manager_view
      tools:
        - filesystem.read_file
        - filesystem.write_file
        - filesystem.list_directory
        - filesystem.search_files
        - memory.search
        - memory.remember
        - task.assign
        - team.status
      # Triage on a weekday loop. `cronJobs` is recipe sugar — it becomes
      # `wake.schedules` on the agent yaml.
      cronJobs:
        - id: triage
          schedule: "*/30 7-23 * * 1-5"
          enabledByDefault: false
          message: >-
            Triage loop. Read notes/status.md and the board. Turn anything new into a
            ticket in `backlog` with an explicit acceptance check. Assign the highest
            value unblocked ticket. If a ticket in `ready-for-pr` has QA evidence,
            open the PR (this needs approval). Never move a ticket out of `testing`
            yourself — that lane belongs to QA.
      permissions:
        network: {mode: ask}
        shell: {mode: deny}
      wake:
        accepts_agent_requests: true

  - role: dev
    required: true
    agent:
      name: Software Engineer
      role: >-
        Implements assigned tickets end to end, leaves the tree consistent, and hands
        finished work to QA. Does not open pull requests.
      description: Engineer on the development team.
      model: profile:default
      memory_scope: manager_view
      tools:
        - filesystem.read_file
        - filesystem.write_file
        - filesystem.list_directory
        - filesystem.search_files
        - memory.search
        - memory.remember
        - task.assign
      cronJobs:
        - id: work
          schedule: "*/30 7-23 * * 1-5"
          enabledByDefault: false
          message: >-
            Work loop. Take your assigned ticket in `in-progress`. Finish it, or finish a
            self-contained piece of it and write down exactly what remains. When it is
            ready, move the ticket to `testing`, assign QA, and record how to verify it.
      permissions:
        network: {mode: ask}
        shell: {mode: deny}
      wake:
        accepts_agent_requests: true

  - role: devops
    required: false
    agent:
      name: DevOps
      role: >-
        Owns build, deploy and environment tickets. Same rule as dev: hands work to QA,
        does not open pull requests.
      description: DevOps/SRE on the development team.
      model: profile:default
      memory_scope: manager_view
      tools:
        - filesystem.read_file
        - filesystem.write_file
        - filesystem.list_directory
        - memory.search
        - memory.remember
        - task.assign
      cronJobs:
        - id: work
          schedule: "0 8-18 * * 1-5"
          enabledByDefault: false
          message: >-
            Work loop. Take your assigned infrastructure ticket, complete it, and move it
            to `testing` with the verification steps written down. Anything that deploys
            needs an approval — ask rather than assume.
      permissions:
        network: {mode: ask}
        shell: {mode: deny}
      wake:
        accepts_agent_requests: true

  - role: test
    required: true
    agent:
      name: QA
      role: >-
        Verifies tickets in `testing` against their acceptance checks and decides pass or
        fail. The only role that can move work out of testing.
      description: QA/test engineer on the development team.
      model: profile:default
      memory_scope: manager_view
      tools:
        - filesystem.read_file
        - filesystem.list_directory
        - filesystem.search_files
        - memory.search
        - memory.remember
        - task.assign
      cronJobs:
        - id: qa
          schedule: "*/30 7-23 * * 1-5"
          enabledByDefault: false
          message: >-
            QA loop. Drain `testing`. For each ticket, run its acceptance check and record
            the evidence. On pass, move it to `ready-for-pr` and hand to the lead. On fail,
            move it back to `in-progress`, hand to the author, and say exactly what broke.
      permissions:
        # QA reads and reports; it does not edit the tree it is judging.
        network: {mode: ask}
        shell: {mode: deny}
      wake:
        accepts_agent_requests: true

workflows:
  - id: ship_a_change
    name: Ship a change
    purpose: Take one ticket from implemented to PR-ready, with QA and a human gate before the PR.
    status: draft
    trigger:
      manual: true
    nodes:
      - id: summarize_change
        type: llm
        agent: "{{teamId}}-dev"
        input:
          prompt: >-
            Summarize the change on this ticket: what changed, why, and how a reviewer
            verifies it. Be specific about files and commands.
        output_fields:
          - {name: summary, type: text, description: the change summary}
          - {name: verification, type: text, description: how to verify it}
        output: change_summary
      - id: qa_verdict
        type: llm
        agent: "{{teamId}}-test"
        input:
          prompt: "Given this change and its verification steps, does it pass QA? ${change_summary}"
        output_fields:
          - {name: verdict, type: text, description: PASS or FAIL with evidence}
        output: qa
      - id: approve_pr
        type: human_approval
        input:
          prompt: "QA said: ${qa}. Open the PR?"
      - id: record
        type: writeback
        input:
          content: "${change_summary}\n\nQA: ${qa}"
          path: notes/status.md
    edges:
      - {from: summarize_change, to: qa_verdict, on: success}
      - {from: qa_verdict, to: approve_pr, on: success}
      - {from: approve_pr, to: record, on: success}

files:
  - path: notes/engineering-conventions.md
    template: conventions
  - path: shared-context/definition-of-done.md
    template: definitionOfDone

templates:
  conventions: |
    # Engineering conventions — {{teamName}}

    _Lead-curated. The team reads this every wake; keep it short and true._

    - One ticket per agent at a time. Finish or hand back — no silent parking.
    - A ticket is not started until it has an acceptance check someone else could run.
    - Dev and DevOps never open pull requests. QA verifies first; the lead opens it.
    - Leave the tree consistent at the end of every session, even a partial one.
    - Write what you learned into team memory (`memory.remember`), not into a comment
      nobody will read again.

  definitionOfDone: |
    # Definition of done — {{teamName}}

    A ticket may move to `ready-for-pr` only when all of these are true:

    1. The acceptance check in the ticket has been run, and the evidence is recorded.
    2. Tests covering the change exist and pass.
    3. The change is described well enough that a reviewer does not have to reconstruct it.
    4. Anything deliberately left undone is written down as a follow-up ticket.
---

# Development Team

A small engineering team that works a shared ticket board: the **lead** triages and
assigns, **dev** and **devops** build, **QA** verifies, and only then does the lead
open a pull request.

Ported from the ClawRecipes `development-team` recipe. The interesting part of the
port is what stopped being prose:

| ClawRecipes | Here |
|---|---|
| ticket files under `work/{backlog,in-progress,testing,done}` | `lanes:` — a real board, `jigga team lanes development_team` |
| "QA must verify before a PR" repeated in five prompts | `gate:` on the `testing` and `ready-for-pr` lanes; only QA moves work out of testing |
| `scripts/ticket-hygiene.sh` policing lane/owner drift | not needed — the lane vocabulary is validated by the runtime |
| a `workflow-runner` agent | not needed — the supervisor is the runner |
| per-role cron loops | the same loops, as `cronJobs:` sugar → `wake.schedules` |

## Install

```bash
jigga recipes scaffold development-team                 # as `development_team`
jigga recipes scaffold development-team --id acme-eng   # or under your own id
```

Then give it work:

```bash
# File a ticket onto the board (lands on `backlog`)
jigga task create --title "Fix the flaky login test" --team development_team \
                  --assignee development_team-dev

jigga team lanes development_team          # the board
jigga task list --lane testing             # what QA is holding
jigga team decisions development_team      # who handed what to whom, and why

# Move it, as someone. The gate is real:
jigga task move <task-id> testing      --as development_team-dev    # allowed
jigga task move <task-id> ready-for-pr --as development_team-dev    # refused
jigga task move <task-id> ready-for-pr --as development_team-test   # QA's call
```

## Turning the work loops on

Every role ships a `cronJobs` loop that is **off by default** (`enabledByDefault:
false`), because a freshly installed example should not start waking four agents
every half hour on its own. Turn on the ones you want:

```bash
jigga agents set development_team-lead wake.schedules \
  '[{"cron": "*/30 7-23 * * 1-5", "event": "triage", "message": "Triage loop: …"}]'
```

## The QA gate is enforced, not requested

`lanes.testing.gate: test` means a ticket cannot leave `testing` unless QA moves it, and
`ready-for-pr` is gated to the lead. The handoffs write an auditable decision each time
work changes hands, so "who said this was ready" has an answer that is not a guess.

`open_pull_request`, `force_push` and `deploy` are in `policies.approvals.required_for`,
so they pause for you rather than happening because an agent felt confident.

## Shell is not granted

No agent here has `shell.run`. That is deliberate: a team that can run arbitrary commands
is a different risk conversation, and JIGGA makes you have it on purpose rather than
inheriting it from a template. If this team needs to run builds or tests for real, grant
it to the one agent that needs it and read what you are agreeing to:

```bash
jigga agents set development_team-devops tools '["shell.run", ...]'
jigga agents tools development_team-devops        # what it can actually do now
```

Until then the agents describe the commands to run and a human runs them — which is a
perfectly good first week with a new team.

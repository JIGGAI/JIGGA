---
id: seven_development_team
name: Seven Development Team
kind: team
version: 0.1.0
description: An engineering team — lead, dev, devops, QA — working a portfolio of projects through a ticket board, feature branch by feature branch, with QA before merge.
purpose: Take engineering work from triage to a merged, QA-passed feature branch across whatever projects the team owns, without anyone merging work the tests haven't seen.
memory_scope: manager_view
routing:
  lead: lead
  # The QA gate, as files rather than prose: dev hands to QA, QA hands back to
  # the lead, and only the lead opens the PR. Each handoff writes an auditable
  # decision (`jigga team decisions seven_development_team`).
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
        Triages incoming work into tickets, assigns them, keeps the board honest, and
        merges the feature branch once QA has passed. Creates and scaffolds a new
        project when a request needs one. Does not write the change itself.
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
        - tickets.list
        - tickets.move
        - team.status
        - notifications.send
        - shell.run
        - web.search
        - web.fetch
      notifications:
        channel: default
      # Triage on a weekday loop. `cronJobs` is recipe sugar — it becomes
      # `wake.schedules` on the agent yaml.
      cronJobs:
        - id: triage
          schedule: "*/30 7-23 * * 1-5"
          enabledByDefault: false
          message: >-
            Triage loop. Read notes/status.md and the board. Turn anything new into a
            ticket in `backlog` with an explicit acceptance check, and name the project
            it belongs to. If the request needs a project that does not exist yet,
            create and scaffold it — repository, README, license, CI, the branch
            protection this team's flow assumes — and record where it lives in
            notes/projects.md before assigning work into it. Assign the highest value
            unblocked ticket. If a ticket in `ready-for-pr` has QA evidence, merge its
            feature branch (this needs approval). Never move a ticket out of `testing`
            yourself — that lane belongs to QA. If the lowest in-progress ticket is
            hard blocked, advance the next unblocked one rather than stalling; if a
            ticket has gone quiet for more than a day, say so on it.
        - id: pr-watcher
          schedule: "0 */4 * * *"
          enabledByDefault: false
          message: >-
            PR watcher. For each ticket in `ready-for-pr` and `done` that names a pull
            request, summarize its checks, review state and mergeability onto the
            ticket. When a PR has merged, record the merge commit and delete the feature
            branch. Move a ticket to `done` only when its acceptance check is satisfied
            — a merge on its own is not evidence the work is finished.
        - id: execution
          schedule: "*/30 7-23 * * 1-5"
          enabledByDefault: false
          message: >-
            Execution loop. Drive in-progress tickets to completion and keep
            notes/status.md current. Finish one ticket before starting another. Do not
            reassign a ticket you merely disagree with — leave it where it is, write
            down what you observed, and hand it back to its author if it needs work.
      permissions:
        network: {mode: ask}
        # Every role works in real checkouts, so shell is granted but fenced: the
        # listed commands run, anything else asks rather than fails. The lead's list
        # is the widest — it is the role that stands a new project up from nothing.
        shell:
          mode: restricted
          allow:
            - "git *"
            - "gh *"
            - "npm *"
            - "npx *"
            - "pnpm *"
            - "yarn *"
            - "node *"
            - "python *"
            - "python3 *"
            - "pytest *"
            - "uv *"
            - "make *"
            - "mkdir *"
            - "touch *"
            - "cp *"
            - "mv *"
            - "ls *"
            - "cat *"
            - "grep *"
            - "rg *"
            - "find *"
        filesystem:
          allow:
            - ~/Projects
          deny:
            - ~/.ssh
            - ~/.aws
            - ~/.jigga/secrets
        notifications: send
      wake:
        accepts_agent_requests: true

  - role: dev
    required: true
    agent:
      name: Software Engineer
      role: >-
        Implements assigned tickets end to end on a feature branch, leaves the working
        tree consistent, and hands the branch to QA. Does not merge and does not open
        pull requests.
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
        - tickets.list
        - tickets.move
        - shell.run
        - web.search
        - web.fetch
      cronJobs:
        - id: work
          schedule: "*/30 7-23 * * 1-5"
          enabledByDefault: false
          message: >-
            Work loop. Take your assigned ticket in `in-progress`. Cut a feature branch
            off the project's default branch, named for the ticket, and do the work
            there — never commit to the default branch directly. Finish the ticket, or
            finish a self-contained piece of it and write down exactly what remains.
            Run the acceptance check yourself before handing it on. When it is ready,
            push the branch, record project + branch + commit and how to verify it on
            the ticket, move the ticket to `testing`, and assign QA.
      permissions:
        network: {mode: ask}
        shell:
          mode: restricted
          allow:
            - "git *"
            - "gh *"
            - "npm *"
            - "pnpm *"
            - "yarn *"
            - "node *"
            - "python *"
            - "python3 *"
            - "pytest *"
            - "uv *"
            - "make *"
            - "ls *"
            - "cat *"
            - "grep *"
            - "rg *"
            - "find *"
        filesystem:
          allow:
            - ~/Projects
          deny:
            - ~/.ssh
            - ~/.aws
            - ~/.jigga/secrets
      wake:
        accepts_agent_requests: true

  - role: devops
    required: false
    agent:
      name: DevOps
      role: >-
        Owns build, deploy, CI and environment tickets across the team's projects. Same
        rule as dev: works on a feature branch, hands it to QA, does not merge.
      description: DevOps/SRE on the development team.
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
        - tickets.list
        - tickets.move
        - shell.run
        - web.search
        - web.fetch
      cronJobs:
        - id: work
          schedule: "0 8-18 * * 1-5"
          enabledByDefault: false
          message: >-
            Work loop. Take your assigned infrastructure ticket, complete it on a feature
            branch off the project's default branch, push it, and move the ticket to
            `testing` with project + branch + commit and the verification steps written
            down. Anything that deploys needs an approval — ask rather than assume.
      permissions:
        network: {mode: ask}
        shell:
          mode: restricted
          allow:
            - "git *"
            - "gh *"
            - "npm *"
            - "pnpm *"
            - "yarn *"
            - "node *"
            - "python *"
            - "python3 *"
            - "pytest *"
            - "uv *"
            - "make *"
            - "docker *"
            - "ls *"
            - "cat *"
            - "grep *"
            - "rg *"
            - "find *"
        filesystem:
          allow:
            - ~/Projects
          deny:
            - ~/.ssh
            - ~/.aws
            - ~/.jigga/secrets
      wake:
        accepts_agent_requests: true

  - role: test
    required: true
    agent:
      name: QA
      role: >-
        Checks out the feature branch under test and verifies it against the ticket's
        acceptance check by actually running it, then decides pass or fail. The only
        role that can move work out of testing.
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
        - tickets.list
        - tickets.move
        - shell.run
        - web.fetch
      cronJobs:
        - id: qa
          schedule: "*/30 7-23 * * 1-5"
          enabledByDefault: false
          message: >-
            QA loop. Drain `testing`. For each ticket, check out the feature branch it
            names — in a worktree of your own, not by switching the author's tree — run
            its acceptance check, and record what you actually saw: command and output,
            not a verdict on its own. Verify the branch merges cleanly into the default
            branch before you pass it. On pass, move the ticket to `ready-for-pr` and
            hand to the lead. On fail, move it back to `in-progress`, hand to the
            author, and say exactly what broke and how to reproduce it. Remove your
            worktree when you are done.
      permissions:
        network: {mode: ask}
        # QA runs the acceptance check but does not edit the tree it is judging:
        # `shell.run` without `filesystem.write_file`.
        shell:
          mode: restricted
          allow:
            - "git *"
            - "npm *"
            - "pnpm *"
            - "yarn *"
            - "node *"
            - "python *"
            - "python3 *"
            - "pytest *"
            - "uv *"
            - "make *"
            - "ls *"
            - "cat *"
            - "grep *"
            - "rg *"
            - "find *"
        filesystem:
          allow:
            - ~/Projects
          deny:
            - ~/.ssh
            - ~/.aws
            - ~/.jigga/secrets
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

# `notes/plan.md`, `notes/status.md`, `shared-context/priorities.md`, `TEAM.md`
# and each member's `roles/<id>/SOUL.md` + `MEMORY.md` are NOT listed here on
# purpose: `scaffold_workspace` already writes them, and recipe `files:` are
# create-only, so declaring them again would silently skip.
files:
  - path: TICKETS.md
    template: tickets
  - path: notes/engineering-conventions.md
    template: conventions
  - path: notes/working-in-repositories.md
    template: repo
  - path: notes/projects.md
    template: projects
  - path: notes/qa-access.md
    template: qaAccess
  - path: shared-context/definition-of-done.md
    template: definitionOfDone
  - path: shared-context/memory-policy.md
    template: memoryPolicy
  - path: shared-context/agent-outputs/README.md
    template: agentOutputsReadme

templates:
  tickets: |
    # Tickets — {{teamName}}

    The board is `backlog → in-progress → testing → ready-for-pr → done`, and it is
    real: `jigga team lanes {{teamId}}` shows it, `jigga task move` moves work across
    it, and two of the lanes are gated.

    ## Every ticket carries

    - **Context** — why this exists. A reader who wasn't in the room should follow it.
    - **Project** — which repository this lands in. See `notes/projects.md`.
    - **Requirements** — what must be true when it's finished.
    - **Acceptance check** — a command or procedure *someone else* can run. Not
      "verify it works". A ticket without one is not ready to be picked up.
    - **Owner** — the agent responsible right now.

    Once work starts, it also carries **branch** and **commit** — QA cannot verify a
    change it can't find.

    ## Handoffs

    **Dev/DevOps → QA.** Push the feature branch, then move to `testing` and assign
    QA. Before you do, the ticket must name project, branch and commit, and say how to
    verify the change.

    **QA → Lead (pass).** Record the command you ran and what it printed, confirm the
    branch merges cleanly, move to `ready-for-pr`, assign the lead.

    **QA → Dev (fail).** Move back to `in-progress`, assign the author, and write the
    reproduction. "Doesn't work" is not a bug report.

    **Lead → merge.** Only from `ready-for-pr`, and only with QA evidence on the
    ticket. Opening a pull request is in `policies.approvals.required_for`, so it
    pauses for a human. Link the PR onto the ticket, and delete the branch after it
    lands.

    ## What the runtime enforces for you

    `testing` is gated to QA and `ready-for-pr` to the lead, so "QA before PR" is not a
    convention anyone can quietly skip — a dev trying to move their own work to
    `ready-for-pr` is refused. Each handoff writes a decision you can read back with
    `jigga team decisions {{teamId}}`.

  conventions: |
    # Engineering conventions — {{teamName}}

    _Lead-curated. The team reads this every wake; keep it short and true._

    - One ticket per agent at a time. Finish or hand back — no silent parking.
    - A ticket is not started until it has an acceptance check someone else could run,
      and names the project it belongs to.
    - Every change goes in on a feature branch. Nobody commits to a default branch.
    - Dev and DevOps never merge. QA verifies first; the lead merges.
    - Leave the tree consistent at the end of every session, even a partial one.
    - A branch stays scoped to one ticket. If you find a second problem, file it;
      don't fold it in. If the work genuinely depends on another ticket, say so.
    - Write what you learned into team memory (`memory.remember`), not into a comment
      nobody will read again.

  repo: |
    # Working in repositories — {{teamName}}

    This team works across **many projects**, not one. Every project lives under the
    directory the team is granted (`~/Projects` unless you changed it), and every
    change goes in on a feature branch.

    ## Where the projects live

    The agents are granted `~/Projects` by default. Point them wherever your code
    actually is:

    ```bash
    jigga agents set {{teamId}}-dev permissions.filesystem.allow '["~/code"]'
    jigga agents tools {{teamId}}-dev     # what it can actually do now
    ```

    Keep `notes/projects.md` current — an agent that cannot tell which repository a
    ticket belongs to will guess, and it will guess wrong.

    ## The branch lifecycle

    1. **Branch.** Cut from the project's default branch, named for the ticket
       (`0142-flaky-login-test`). Never commit to the default branch directly.
    2. **Build.** Do the work there. Run the acceptance check yourself before you hand
       it on — QA is a second opinion, not your first test run.
    3. **Hand off.** Push the branch. Put the project, branch, and commit on the
       ticket, along with how to verify it. Move to `testing`, assign QA.
    4. **Verify.** QA checks the branch out in a worktree of its own, runs the check,
       records the output, and confirms it merges cleanly.
    5. **Merge.** The lead merges after QA passes — and only then. Delete the branch.

    ## Don't fight over a working tree

    Several agents in one checkout means a branch switch under someone else's feet is a
    real failure, not a hypothetical one. So:

    - Prefer `git worktree add` per ticket over `git checkout` in a shared tree. It's
      in the allow-list, and it's the difference between two agents working in
      parallel and two agents corrupting each other's build.
    - Never leave a shared tree on someone else's branch.
    - Remove your worktree when the ticket leaves your hands.

    ## Starting a new project

    Only the lead does this, and only from a triaged ticket that asks for it. Create
    the repository, scaffold it, commit the skeleton, and write it into
    `notes/projects.md` before assigning any work into it. A project that exists but
    nobody can find is worse than one that doesn't exist yet.

    ## What shell will and won't run

    Every role has `shell.run` under `permissions.shell.mode: restricted`. The
    allow-list (git, gh, npm/pnpm/yarn, node, python/pytest/uv, make, and read-only
    inspection — plus scaffolding for the lead and docker for devops) runs without
    prompting. Anything outside it **asks** — it is not refused, so an
    unusual-but-legitimate command still gets through with a human nod.

    A short denylist is refused in every mode and cannot be granted around: `rm`,
    `sudo`, `dd`, `mkfs`, `chown -R`, `chmod -R 777`, redirects to `/dev/`, and
    curl/wget piped into a shell. Practically: an agent cannot `rm -rf node_modules`.
    Use `git clean` for that, or do it yourself.

  projects: |
    # Projects — {{teamName}}

    _Lead-curated. Every repository this team works in, and how to get into it._

    Add a project here the moment it exists — a ticket that names a project not on
    this list is a ticket someone will have to come back and ask about.

    ## Template

    ### <project name>
    - **Repository:**
    - **Path:**
    - **Default branch:**
    - **How to run it:**
    - **How to test it:**
    - **Notes:** _(deploy target, who owns it, anything that will surprise a newcomer)_

    ---

    _(no projects yet)_

  qaAccess: |
    # QA access — {{teamName}}

    QA cannot verify what it cannot reach, and a ticket bounced for missing access is
    a wasted round trip. Record here — once — how to reach whatever this team's work
    needs verifying against: base URLs, test accounts, seeded fixtures, how to bring a
    local environment up.

    - Environment:
    - How to start it:
    - Test credentials live in: _(a secret store — not this file)_

    **Never commit real credentials.** `~/.jigga/secrets` is denied to every agent on
    this team on purpose. Name the location; don't paste the value.

  definitionOfDone: |
    # Definition of done — {{teamName}}

    A ticket may move to `ready-for-pr` only when all of these are true:

    1. The acceptance check in the ticket has been run, and the evidence is recorded.
    2. Tests covering the change exist and pass.
    3. The change is described well enough that a reviewer does not have to reconstruct it.
    4. Anything deliberately left undone is written down as a follow-up ticket.

  memoryPolicy: |
    # Memory policy — {{teamName}}

    Chat is not the system of record. The ticket is.

    ## Where things go

    - **The ticket** — the source of truth for one unit of work. Decisions about the
      work belong on the work.
    - **`notes/status.md`** — append-only, short, frequent. A few bullets after each
      session: what changed, what's blocked.
    - **`notes/plan.md`** and **`shared-context/priorities.md`** — lead-curated. If
      you are not the lead, don't edit these; propose through `agent-outputs/` or a
      ticket comment and let the lead fold it in.
    - **`shared-context/agent-outputs/`** — append-only raw material: logs, command
      output, investigation notes. Link it from the ticket rather than pasting it in.
    - **`memory.remember`** — durable team knowledge: a fact that will still be true
      next month, a decision with lasting consequences, a lesson that cost you an
      afternoon. Not a status update.
    - **`roles/<you>/MEMORY.md`** — your own curated notes. Prune what stops being
      true; it is injected into your context every wake.

    ## End of session

    Whatever else you skip, do these:

    1. Update the ticket: what changed, how to verify, how to roll back.
    2. Append a few bullets to `notes/status.md`.
    3. Put the raw output in `agent-outputs/` and reference it from the ticket.

  agentOutputsReadme: |
    # Agent outputs (append-only)

    Raw logs, command output, and investigation notes. Append; don't rewrite.

    Name files `YYYY-MM-DD-topic.md` so they sort.

    This is the place for the long thing — the full test output, the whole stack
    trace, the transcript of what you tried. Link it from the ticket and keep the
    ticket readable.
---

# Seven Development Team

An engineering team that works a portfolio of projects through a ticket board, one
feature branch at a time: the **lead** triages, assigns, and stands up a new project
when a request needs one; **dev** and **devops** build on feature branches; **QA**
checks the branch out and actually runs the acceptance check; and only then does the
lead merge.

Ported from the ClawRecipes `development-team` recipe as it runs in production. The
interesting part of the port is what stopped being prose:

| ClawRecipes | Here |
|---|---|
| ticket files under `work/{backlog,in-progress,testing,done}` | `lanes:` — a real board, `jigga team lanes seven_development_team` |
| "QA must verify before a PR" repeated in five prompts | `gate:` on `testing` and `ready-for-pr`; only QA moves work out of testing |
| ready-for-PR encoded as `Owner: lead` *while still in* the testing lane | its own `ready-for-pr` lane — the state has a name instead of a convention |
| `scripts/ticket-hygiene.sh` policing lane/owner drift | not needed — the lane vocabulary is validated by the runtime |
| `scripts/auto-route-owners.sh` keyword-routing to devops | `routing.handoffs` + `task.assign` |
| `scripts/team-root.sh` resolving the team root before relative paths | not needed — the runtime resolves the workspace |
| a `workflow-runner` agent + its 15-second runner-tick loop | not needed — the supervisor is the runner |
| two overlapping QA loops (`test-work-loop`, `testing-lane-loop`) | one `qa` loop — the lane gate makes the second redundant |
| per-role `SOUL.md` / `AGENTS.md` / `TOOLS.md` written by the recipe | `scaffold_workspace` writes SOUL/MEMORY per member; AGENTS/TOOLS are rendered live from the real roster and grants |
| per-role cron loops | the same loops, as `cronJobs:` sugar → `wake.schedules` |

## Install

```bash
jigga recipes scaffold seven-development-team                 # as `seven_development_team`
jigga recipes scaffold seven-development-team --id acme-eng   # or under your own id
```

Then point it at wherever your projects live — the grant ships as `~/Projects`:

```bash
for role in lead dev devops test; do
  jigga agents set seven_development_team-$role \
    permissions.filesystem.allow '["~/code"]'
done
```

Then give it work:

```bash
# File a ticket onto the board (lands on `backlog`)
jigga task create --title "Fix the flaky login test" --team seven_development_team \
                  --assignee seven_development_team-dev

jigga team lanes seven_development_team    # the board
jigga task list --lane testing             # what QA is holding
jigga team decisions seven_development_team # who handed what to whom, and why

# Move it, as someone. The gate is real:
jigga task move <task-id> testing      --as seven_development_team-dev    # allowed
jigga task move <task-id> ready-for-pr --as seven_development_team-dev    # refused
jigga task move <task-id> ready-for-pr --as seven_development_team-test   # QA's call
```

## Turning the work loops on

Every role ships `cronJobs` loops that are **off by default**
(`enabledByDefault: false`), because a freshly installed example should not start
waking four agents every half hour on its own. Six loops are defined: `triage`,
`pr-watcher` and `execution` on the lead, `work` on dev and on devops, and `qa` on
test. Turn on the ones you want:

```bash
jigga agents set seven_development_team-lead wake.schedules \
  '[{"cron": "*/30 7-23 * * 1-5", "event": "triage", "message": "Triage loop: …"}]'
```

On the ClawRecipes team this was ported from, the lead/dev/devops/test work loops and
a backup job run enabled; `execution`, `pr-watcher` and the testing-lane sweep sit
disabled. Start with `triage` and one `work` loop and add from there — four agents
waking every thirty minutes is a lot of activity to read at once.

## The QA gate is enforced, not requested

`lanes.testing.gate: test` means a ticket cannot leave `testing` unless QA moves it,
and `ready-for-pr` is gated to the lead. The handoffs write an auditable decision each
time work changes hands, so "who said this was ready" has an answer that is not a
guess.

`open_pull_request`, `force_push` and `deploy` are in `policies.approvals.required_for`,
so they pause for you rather than happening because an agent felt confident.

## Shell is granted, and fenced

Unlike the `development-team` example, this team **does** get `shell.run` — every role
works in real checkouts, and a QA agent that cannot run the acceptance check can only
tell you what it would have run.

The grant is fenced rather than open. `permissions.shell.mode: restricted` means the
allow-listed commands (git, gh, npm/pnpm/yarn, node, python/pytest/uv, make, plus
read-only inspection; docker for devops) run without prompting, and **anything else
asks** — it is not refused, so an unusual but legitimate command still gets through
with a human nod. Widen it per role as you learn what this team actually needs:

```bash
jigga agents set seven_development_team-devops permissions.shell.allow \
  '["git *", "terraform *", …]'
jigga agents tools seven_development_team-devops   # what it can actually do now
```

Two things the allow-list cannot buy back. A dangerous-pattern denylist — `rm`,
`sudo`, `dd`, `mkfs`, `chown -R`, `chmod -R 777`, writes to `/dev/`, curl/wget piped
into a shell — is refused in every mode, including `allow`, so an agent cannot
`rm -rf node_modules` no matter how it is configured. And QA has `shell.run` but not
`filesystem.write_file`: it runs the check, it does not edit the tree it is judging.

## Many projects, one team

The team works across whatever repositories it owns, so two files carry the weight
that a single-repo team wouldn't need. `notes/projects.md` is the lead-curated index —
repository, path, default branch, how to run it, how to test it. A ticket that names a
project missing from that list is a ticket someone has to come back and ask about.

`notes/working-in-repositories.md` is the branch discipline: cut from the default
branch, never commit to it directly, run your own acceptance check before handing off,
and prefer `git worktree add` over `git checkout` when more than one agent is in the
same repository. That last one is the failure most likely to actually bite — two
agents in one working tree is a corrupted build, not a merge conflict.

New projects are the lead's job alone, and only from a triaged ticket that asks for
one: create the repository, scaffold it, commit the skeleton, and write it into
`notes/projects.md` before assigning work into it.

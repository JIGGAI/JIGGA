# Building & Managing Agents and Teams in JIGGA

A practical guide to how you create agents and teams, and how JIGGA executes and
manages them. Everything is **file-first** under `~/.jigga/` — durable, grep-able,
git-friendly, no hidden app state. (Inspired by `~/ClawRecipes`; this is the
Python runtime version.)

---

## 1. The mental model

| Thing | What it is | Lives in |
|---|---|---|
| **Agent** | A worker with a role, a model, tools, and permissions | `~/.jigga/agents/<id>.yaml` |
| **Team** | A named group of agents with a lead and routing | `~/.jigga/teams/<id>.yaml` |
| **Capability** | An action an agent can invoke (a tool / workflow step) | registry (bundled + opt-in + local) |
| **Workspace** | A team's (or solo agent's) shared file area | `~/.jigga/workspaces/<id>/` |
| **Recipe** | A template that *generates* a team/agent | `~/.jigga/recipes/*.md` or `examples/recipes/` |
| **Supervisor** | The always-on daemon that drives everything | `jigga supervisor run` |
| **Task** | A unit of work assigned to an agent | `~/.jigga/tasks/` |

The flow, end to end:

```
recipe ──scaffold──▶ agents + team + workspace          (BUILD)
                                │
   schedules / channels / CLI ──┤                        (TRIGGERS)
                                ▼
        supervisor tick ──▶ creates tasks ──▶ run_agent  (MANAGEMENT)
                                                  │
              read plan/priorities ──▶ model loop (tools, gated) ──▶ append outputs
                                                  │
                       needs approval? ──▶ ask on channel ──▶ approve <code> ──▶ resume
```

---

## 1.5 First-run setup — your default agent

`jigga setup` (also offered by `jigga init`) is a short wizard: it asks **who the
AI works with** and **what this install is for**, lets you pick **chief of staff
vs personal assistant** and a **communication style**, then — from your answers,
nothing hardcoded — writes your `~/.jigga/USER.md` and scaffolds the **default
agent**.

The default agent (`default: true`) is the **catch-all** for any inbound message
not routed to a specific agent, and your direct assistant. It's granted **all
capabilities + cross-team read access** (`team.list`, `team.status`) plus the
dispatch tools (`team.run`, `task.assign`), so it can oversee and run the whole
org. Chief-of-staff delegates aggressively (route/run teams, don't do specialist
work); personal-assistant handles small requests itself and delegates the rest.
Re-run anytime with `jigga setup --overwrite`.

---

## 2. BUILD — three ways to create agents/teams

### a) Scaffold from a recipe (recommended)
A **recipe** is a Markdown file with YAML frontmatter describing a team and its
roles. Scaffolding generates the agent + team YAML and the workspace:

```bash
jigga recipes list                                  # list available recipes
jigga recipes scaffold marketing-team --id acme     # generate the team
```
This writes `acme-lead`, `acme-copywriter`, `acme-editor` agents, the `acme`
team, and `~/.jigga/workspaces/acme/`. `{{teamId}}`/`{{teamName}}` in the recipe
are templated. Re-running is safe (create-only); pass `--overwrite` to regenerate.

### b) Copy the bundled examples
```bash
jigga init --examples        # scaffolds the bundled example recipes into ~/.jigga
```

### c) Hand-write YAML
Drop a file in `~/.jigga/agents/` or `~/.jigga/teams/`. No command needed — the
runtime picks it up, and the workspace is created on first run (see §4).

---

## 3. The shapes

### Agent (`~/.jigga/agents/<id>.yaml`)
```yaml
id: acme-lead
name: Acme Lead
role: Distills the product into a sharp launch message.   # the system prompt
memory_scope: task_only
model: profile:default            # profile:default | gpt-5.5 | profile:<name>
permission_mode: ask              # plan_only | ask | accept_edits | autonomous | locked_down
tools: [draft_with_model]         # capability ACTIONS this agent may call
permissions:
  network: {mode: ask}
  shell: {mode: deny}
wake:                             # optional scheduled work-loops (see §5)
  schedules:
    - cron: "*/30 7-23 * * 1-5"
      event: triage
      message: "Triage loop: review plan/priorities, update notes/status.md."
```

### Team (`~/.jigga/teams/<id>.yaml`)
```yaml
id: acme
name: Acme Marketing
purpose: Turn a product brief into reviewed launch copy.
agents:
  - {id: acme-lead, role: lead, required: true}
  - {id: acme-copywriter, role: drafting, required: true}
  - {id: acme-editor, role: review, required: true}
routing:
  default_assignee: acme-lead     # the lead / workspace curator
  handoffs:                       # who picks up after whom (see §4)
    - {from: acme-lead,       to: acme-copywriter, when: brief_ready}
    - {from: acme-copywriter, to: acme-editor,     when: draft_ready}
```

### Capabilities (what `tools:` references)
- **Bundled** (always on): `filesystem.*`, `notifications.send`, `summarize_*`,
  `spawn_subagent`, `content-drafting` actions, `draft_with_model` (real model call).
- **Opt-in first-party**: `gog` (Gmail/Workspace), `google-calendar`, `telegram` —
  installed via `jigga capabilities install <name>`.
- **User/project-local**: your own packs in `~/.jigga/capabilities/`.

---

## 4. MANAGEMENT — how work actually runs

The **supervisor** is the always-on engine. Run it as a process (a real install
runs it as a service):
```bash
jigga supervisor run
```
Each tick it:
1. **rotates** the audit log (by day/size, prunes old archives);
2. **polls enabled channels** → turns inbound messages into tasks;
3. **fires due schedules** → cron-due agents get a task (with the work-loop message);
4. **runs agents** that have pending tasks.

When an agent runs (`run_agent`):
- its **workspace is ensured** (created on first use — team members bind to the
  team workspace, solo agents get their own);
- the lead-curated **`plan.md` + `priorities.md`** are read into its prompt;
- the **tool-use loop** runs: the model decides which tools (capabilities) to
  call; each call is **gated** by the agent's permissions, the capability's risk
  level, and `permission_mode`;
- on completion, the result is **appended** to `shared-context/agent-outputs/<agent>.md`
  and a line to `notes/status.md` (the **read → act → write** loop).

### The shared workspace (`~/.jigga/workspaces/<team>/`)
```
TEAM.md                        # name / purpose / members / lead
notes/plan.md                  # lead-curated (create-only)
notes/status.md                # append-only operational log
shared-context/priorities.md   # lead-curated (create-only)
shared-context/agent-outputs/  # append-only, per-member outputs
shared-context/feedback/       # append-only QA / feedback
roles/<member>/SOUL.md         # persona (authored — edit to give a voice)
roles/<member>/MEMORY.md       # the member's curated long-term memory
roles/<member>/memory/<date>.md# dated daily breadcrumbs (written each run)
```
**Curator model:** only the **lead** edits `plan.md`/`priorities.md`; other
members **append** to `agent-outputs/`/`feedback/`. They coordinate through
files, not direct messages.

### What an agent knows when it wakes (the context pack)
Each run, JIGGA assembles the agent's system prompt from layered files (the
OpenClaw/ClawRecipes model) so it isn't a per-task amnesiac:

`USER.md` (the principal — `~/.jigga/USER.md`) → identity (from config) →
`SOUL.md` (persona) → role + teammates roster → `TEAM.md` → tools → its
`MEMORY.md` + recent daily logs + team facts → the lead's plan/priorities → the task.

Each layer is **generate-unless-authored**: `AGENTS`/`TOOLS` are generated from
config; drop a `roles/<id>/AGENTS.md` or `TOOLS.md` to override. `USER.md` and
`SOUL.md` are authored (starters are scaffolded — edit them). Missing layers are
skipped. **Privacy:** in a group/shared channel (`restricted_memory`), the private
`USER.md` and `MEMORY.md` layers are withheld so personal context can't leak.

### Talking to one agent vs the team
- **One agent:** assign it a task (a channel message routed to it, a schedule, or
  `jigga task create … --assignee <id>`).
- **The team collaborating:** a workflow chains steps across members (e.g. the
  `team_launch` workflow: lead → copywriter → editor, each a real model call
  wired by named outputs), or the lead delegates.

### Handoffs (member → member, file-first)
`routing.handoffs` makes the team self-advancing. When a `from` member
**completes its team task**, JIGGA creates the next task for each `to` member —
completion is the signal, so every outgoing handoff from that member fires. The
supervisor's normal tick then runs the next member, and the chain continues.

Every handoff is recorded in an auditable file —
`workspaces/<team>/shared-context/handoffs.jsonl` (who, to whom, `when`, the new
task id, optional evidence) — and emitted as a `team.handoff.fired` audit event.
No ephemeral message bus; coordination is files you can read.

A hop counter on each handoff task caps the chain at `teams.handoff_max_hops`
(default 25) so a cyclic routing graph can't loop forever (refusals log as
`team.handoff.blocked`).

```bash
jigga team handoff acme --from acme-lead --signal brief_ready   # fire manually
jigga team decisions acme                                       # read the log
```

### Mailbox (free-form messages, file-first)
Handoffs route *structured* work; the **mailbox** carries everything else —
ad-hoc notes, questions, FYIs — agent→agent or human→agent. One JSON file per
message in the recipient's workspace inbox
(`workspaces/<team>/roles/<member>/inbox/<msg_id>.json`); nothing ephemeral.

The delivery loop:
1. Send: the `mailbox.send` capability (payload `{to, body, subject?}` —
   delivery resolves the **recipient's** home workspace, so cross-team sends
   land where the recipient wakes), or as a human:
   ```bash
   jigga mailbox send assistant --body "review the aurora draft" --subject brief
   jigga mailbox list assistant --unread
   ```
2. Wake: an unread message **wakes its recipient within a tick** (~30s) — the
   supervisor queues a check-your-inbox task, subject to the normal per-agent
   wake throttle, so two agents can't ping each other into a loop.
3. Read: unread messages appear in the recipient's context pack ("Your inbox",
   a private layer — group/channel sessions never see it). After a
   **successful** run they're marked read (`read_at` annotated in place); a
   failed run re-sees them next wake.

Messages are never moved or deleted — the inbox is a complete correspondence
record, greppable and searchable via `memory.search`. Audit events:
`mailbox.sent`, `mailbox.read`, `supervisor.mail_wake`.

### Approvals (human-in-the-loop)
A medium/high-risk action by a non-`autonomous` agent **pauses** for approval:
JIGGA parks a code-gated approval and asks on the originating channel. Reply
`approve <code>` / `deny <code>` (or use `jigga approvals approve <code>`) and the
held task resumes.

---

## 5. Scheduled work-loops (`cronJobs`)

A recipe role can declare scheduled "work loops" — the agent wakes on a cron and
gets the `message` as its task:
```yaml
agents:
  - role: lead
    tools: [draft_with_model]
    cronJobs:
      - id: triage-loop
        schedule: "*/30 7-23 * * 1-5"   # 5-field cron
        enabledByDefault: false         # safe-idle: off unless you turn it on
        message: "Triage loop: review plan/priorities and new tasks; update status.md."
```
`enabledByDefault: false` loops are **not** scheduled at scaffold time (safe by
default) — flip to `true`, or add a `wake.schedule` to the scaffolded agent.

---

## 6. Example recipes

### A team (`examples/recipes/marketing-team.md`)
```markdown
---
id: marketing-team
name: Marketing Team
kind: team
purpose: Turn a product brief into reviewed launch copy.
routing:
  lead: lead
agents:
  - role: lead
    name: "{{teamName}} Lead"
    description: Distills the product into a sharp launch message and angle.
    tools: [draft_with_model]
  - role: copywriter
    name: Copywriter
    description: Writes punchy launch copy for indie devs; no hashtags, no emoji.
    tools: [draft_with_model]
  - role: editor
    name: SEO Editor
    description: Reviews copy for clarity, claims, and keyword coverage.
    tools: [draft_with_model]
---

# Marketing Team
Scaffolds lead → copywriter → editor. Each role becomes `{{teamId}}-<role>`.
```
```bash
jigga recipes scaffold marketing-team --id acme
```

### A single agent (`examples/recipes/researcher.md`)
```markdown
---
id: researcher
name: Researcher
kind: agent
model: profile:default
tools: [summarize_relevant_context]
cronJobs:
  - id: morning-research
    schedule: "0 8 * * 1-5"
    enabledByDefault: true
    message: "Morning research loop: produce a short briefing in your workspace."
---

# Researcher (single-agent recipe)
```
```bash
jigga recipes scaffold researcher --id my-researcher
```

---

## 7. End-to-end walkthrough

```bash
# 0. one-time setup
jigga init
jigga model setup            # pick ChatGPT subscription / API key / dry-run
jigga channels setup         # (optional) wire Telegram so the team is reachable

# 1. build a team from a recipe
jigga recipes scaffold marketing-team --id acme
jigga team workspace acme    # see the scaffolded workspace files

# 2. give the team direction (lead curates the plan)
#    edit ~/.jigga/workspaces/acme/notes/plan.md and shared-context/priorities.md

# 3. run it
jigga supervisor run         # always-on: schedules + channels + tasks
#   …or one-shot a workflow:
jigga workflow run team_launch

# 4. observe
jigga trace <id>             # the whole causal tree (tick → agent → tool → subagent)
jigga cost                   # per-agent token usage / spend (and budget status)
jigga approvals list         # anything waiting on you
```

---

## 8. Command reference

| Area | Commands |
|---|---|
| Build | `jigga init [--examples]`, `jigga recipes list|show|scaffold <recipe> --id <id> [--overwrite]`, `jigga team init <id>`, `jigga team workspace <id>` |
| Model | `jigga model setup`, `jigga model use <provider>`, `jigga model login [--device-code]`, `jigga model status` |
| Channels | `jigga channels setup`, `jigga channels status`, `jigga channels listen` |
| Run | `jigga supervisor run`, `jigga team run <id>`, `jigga workflow run <id>`, `jigga run agent <id>` |
| Coordination | `jigga team handoff <id> --from <member> [--signal S]`, `jigga team decisions <id>` |
| Human-in-the-loop | `jigga approvals list`, `jigga approvals approve <code>`, `jigga approvals deny <code>` |
| Observability | `jigga trace <id>`, `jigga cost [--since 7d]`, `jigga logs tail`, `jigga audit [--agent X --type T --since 24h]` |
| Capabilities | `jigga capabilities list`, `jigga capabilities install <name>` |

See also: `docs/ARCHITECTURE.md`, `docs/MODEL_BACKED_WORKFLOWS.md`,
`docs/CHATGPT_OAUTH_PROVIDER.md`, `docs/tools/CHANNEL_GATEWAY_MESSAGE_ADAPTERS.md`,
and `docs/ROADMAP_TO_PRODUCTION.md`.

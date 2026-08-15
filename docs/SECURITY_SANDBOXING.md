# Security and Sandboxing

## Definition

Sandboxing in JIGGA means enforcing hard boundaries on what agents can see, access, execute, and persist.

Docker or containers can be part of sandboxing, but sandboxing is broader than containers.

A real agent sandbox includes:

- memory access control
- filesystem access control
- tool restrictions
- network limits
- shell limits
- approval gates
- audit logs
- optional process/container/VM isolation

## Main Risk

The biggest risk is not only code execution.

The biggest risk is data access.

An agent does not need root access to cause harm. It may only need access to:

- private notes
- email
- calendar
- API keys
- credentials
- browser sessions
- SSH keys
- sensitive project files

## Attribution — who did this

Every audited event carries a top-level `actor`. The format is a prefixed
label, so machine and human separate on a prefix match:

| Actor | Meaning |
|---|---|
| `user` | a person, at the CLI |
| `user:<channel>` | a person, over a channel they messaged from |
| `agent:<id>` | an agent's own turn |
| `workflow:<id>` | a workflow run executing its steps |
| `supervisor` | the heartbeat, acting on no one's direct instruction |
| `system` | unattributed — a real answer meaning "nothing claimed this", and a bug wherever it appears on a mutation |

```
jigga audit --actor human          # everything a person did, however they reached JIGGA
jigga audit --actor machine        # everything JIGGA did on its own
jigga audit --actor agent          # the family
jigga audit --actor agent:chief    # one agent
```

**Innermost wins.** A supervisor tick that wakes an agent attributes that
agent's actions to the agent, because that is who performed them. What a human
*initiated* is a different question with a different answer, recoverable from
the trace root — `jigga trace <id>` shows the whole causal tree, and its first
event carries `user`.

Approvals additionally record `resolved_by` and `resolved_by_human` on the
approval record itself, since an approval's entire purpose is that a person
made it.

This exists because the precursor stack couldn't answer it. When 22 posts
vanished (FIELD_LESSONS §3.6), the automation had written through the same API
as the humans and `created_by` was the constant `dashboard-ui` for every row;
the only forensic tool left was diffing hourly SQLite snapshots to bound the
window, and the culprit was permanently unattributable.

## Tool Grants — deny by default

**An agent may invoke only the actions it has been explicitly granted.** There
is no implicit grant, no "all bundled capabilities" default, and no execution
path that skips the check. An agent with no `tools:` can do nothing.

```yaml
# agents/researcher.yaml
tools:                      # the grant list — this IS the boundary
  - memory.search
  - web.fetch
permissions:
  tools:
    allow:                  # equivalent second source, merged with `tools:`
      - summarize_day
```

Enforced at three layers, each independent:

| Layer | What it does |
|---|---|
| `agent.py::_resolve_agent_actions` | The model is only *offered* function schemas for granted actions |
| `workflow.py::_step_policy` | A workflow step naming an ungranted action is `blocked`, at plan time and at run time |
| `dispatcher.py::dispatch_action` | Final floor before any handler runs — raises `PermissionError` and audits `capability.invocation.denied` |

The floor exists because the first two are caller-side. Historically the grant
list gated only the model-facing menu, so anything naming an action directly —
a workflow node, a recipe, a scheduled job — reached the handler regardless. An
agent with `tools: []` could write files through a workflow while its tool list
showed nothing. The floor means a future caller that forgets cannot reintroduce
that gap.

Grant denial precedes risk level, `permission_mode`, and resource permissions:
no `autonomous` mode and no open filesystem policy can talk an ungranted action
into running. The one carve-out is **agent-less dispatch** — the engine acting
on its own behalf (workspace writeback and similar), which is not an agent
exercising authority, and is the same carve-out the runtime-only check makes.

### Command-line access is guarded twice

`shell.run` is not offered by any setup wizard at any setting — turning it on
requires a deliberate hand-edit of the agent's yaml. Even then the grant alone
is not enough: `permissions.shell` must independently permit the command, or
`safe_process` refuses it. Granting the tool and allowing the command are two
separate decisions, and both are required.

## Permission Model

Example:

```yaml
permissions:
  memory:
    scope: manager_view
  filesystem:
    allow:
      - ~/Projects
      - ~/.jigga/memory/summaries
    deny:
      - ~/.ssh
      - ~/Library/Keychains
      - ~/.aws
  network:
    mode: ask
  shell:
    mode: restricted
  calendar: read
  email: read
  notifications: send
```

## Permission Modes

### allow

Agent can perform the action without additional approval.

### ask

Agent must request user approval.

### deny

Agent cannot perform the action.

### restricted

Agent may use a constrained version of the action.

## Sandboxing Layers

### 1. Memory Sandbox

Controls which memory scope the agent can access.

This is the most important layer for personal AI workers.

### 2. Filesystem Sandbox

Controls which paths agents can read and write.

Defaults should be deny-by-default with explicit allowlists.

### 3. Tool Sandbox

Agents should prefer structured tools over raw shell access.

Better:

```text
read_file(path)
write_file(path, content)
run_tests(project)
search_email(query)
```

Riskier:

```text
raw shell
unrestricted browser control
unrestricted network requests
```

### 4. Execution Sandbox

Optional isolation environment.

Levels:

1. Process-level restrictions
2. Container isolation
3. MicroVM isolation
4. Separate physical or virtual machine

JIGGA should start with policy sandboxing and simple process/container isolation. MicroVMs can be a later hardening layer.

## Default Security Posture

Recommended defaults:

- no raw shell by default
- no access to secrets by default
- no broad home directory access by default
- no autonomous recurring workflow activation without approval
- no external publishing without approval
- no credential access except through approved tools
- all memory writes logged
- all sensitive tool calls logged

## Approval Gates

Some actions should require approval by default:

- sending emails
- publishing content
- deleting files
- modifying source control branches
- creating recurring workflows
- changing permissions
- accessing sensitive directories
- making purchases
- sending external network requests in restricted contexts

## Audit Logs

JIGGA should log:

- agent wakes
- tasks created
- workflow invocations
- memory reads/writes
- tool calls
- permission denials
- approvals
- filesystem writes
- external actions

Example log:

```yaml
event: tool_call
agent: publisher
workflow: social_content_syndication
tool: notifications.send
time: 2026-04-23T12:00:00-04:00
status: approved
```

## Loop Prevention

Security also includes preventing runaway autonomy.

Controls:

- max wake count per interval
- max delegation depth
- max workflow recursion
- cooldowns
- duplicate task detection
- failed task backoff
- human approval for high-risk loops

## Secrets Handling

Agents should not read secrets directly from the filesystem.

Preferred approach:

- use a secrets broker
- expose named capabilities, not raw values
- log capability usage without revealing secret values
- require approval for new secret access

## V1 Security Scope

Minimum viable security for V1:

- memory scopes
- filesystem allow/deny lists
- restricted shell mode
- approval gates
- audit logs
- no autonomous workflow activation without approval

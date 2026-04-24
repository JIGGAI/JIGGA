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

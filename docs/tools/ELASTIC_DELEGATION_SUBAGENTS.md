# Elastic Delegation & Subagent Spawning

## Purpose

Elastic delegation lets a primary agent temporarily spin up bounded subagents when a task is too large, too specialized, or too parallelizable for one agent to complete efficiently.

This gives JIGGA the ability to behave like a small team of workers without requiring every worker to run continuously.

> A primary agent remains accountable for the task outcome. Subagents are temporary execution workers with limited scope, limited permissions, and required result reporting.

---

## Product Definition

**Elastic delegation** is the ability for an agent to break work into smaller work orders and invoke temporary subagents through execution backends such as Codex, Claude Code, or future local/cloud agent runtimes.

This is similar to a manager assigning focused work to temporary specialists.

A subagent may:

- inspect a limited part of the repo
- implement a scoped change
- write tests
- review code
- summarize findings
- generate artifacts
- report risks or blockers

A subagent should not:

- receive full personal memory by default
- operate outside its assigned scope
- silently persist new memory
- spawn its own subagents in v1
- bypass the parent agent's review

---

## Core Mental Model

```text
Supervisor Daemon
  ↓
Primary Agent
  ↓ detects task is too large / parallelizable / specialized
Delegation Planner
  ↓
Subagent Pool
  ├─ codex_cli: implement feature slice
  ├─ claude_code: review architecture
  ├─ codex_cli: write tests
  └─ claude_code: inspect security risks
  ↓
Result Aggregator
  ↓
Primary Agent Review
  ↓
Memory + Task State + Artifacts
```

---

## Core Concepts

### Primary Agent

The agent responsible for the final outcome.

The primary agent:

- owns the parent task
- decides whether delegation is needed
- creates work orders
- launches subagents through controlled tools
- reviews subagent output
- merges or rejects results
- writes approved outcomes to memory

### Subagent

A temporary worker created for a bounded task.

A subagent:

- gets a specific work order
- receives scoped memory only
- operates under explicit permissions
- returns structured output
- terminates when finished

### Delegation Planner

The planning layer that turns one large task into smaller work orders.

It decides:

- whether delegation is allowed
- how many subagents are needed
- what each subagent should do
- which backend each subagent should use
- what files, tools, and memory each subagent can access

### Runtime Adapter

A backend-specific implementation that launches an external execution environment.

Examples:

- `codex_cli`
- `codex_cloud`
- `claude_code`
- `local_agent_runtime`
- `containerized_agent`

### Session Manager

Tracks subagent lifecycle.

Responsibilities:

- create sessions
- attach logs
- stream output
- enforce timeouts
- collect artifacts
- mark completion/failure
- kill runaway sessions

### Result Aggregator

Collects subagent outputs and prepares them for parent review.

It should produce:

- summary of work completed
- changed files
- test results
- unresolved risks
- conflicts between subagents
- suggested next actions

---

## Why This Should Be a Tool

Subagent spawning should be exposed to agents as a controlled tool, not as raw shell access.

The parent agent should not directly run:

```bash
claude "do this task"
codex exec "do this task"
```

Instead, it should call a structured tool:

```yaml
tool: spawn_subagent
```

This allows JIGGA to enforce:

- permissions
- memory scopes
- depth limits
- parallelism limits
- audit logs
- review gates
- cost limits
- sandboxing

---

## Tool Definition

### Tool Name

```yaml
spawn_subagent
```

### Tool Purpose

Launch a temporary bounded subagent using an approved backend.

### Tool Input

```yaml
spawn_subagent:
  backend: codex_cli
  mode: execute
  parent_agent_id: engineer
  task_id: task_123
  work_order:
    goal: "Write validation tests for the auth/session module"
    instructions: |
      Add focused tests for session expiration, invalid tokens, and refresh behavior.
      Do not modify production code unless tests reveal a clear bug.
  cwd: ./apps/api
  memory_scope: project_view_minimal
  permissions:
    filesystem:
      allow:
        - apps/api/src/auth/**
        - apps/api/tests/auth/**
      deny:
        - .env
        - ~/.ssh
    network:
      mode: disabled
    shell:
      mode: restricted
  limits:
    max_runtime_minutes: 20
    max_files_changed: 8
    max_output_tokens: 8000
  output_required:
    - summary
    - changed_files
    - commands_run
    - test_results
    - risks
```

### Tool Output

```yaml
subagent_result:
  session_id: subagent_sess_456
  backend: codex_cli
  status: completed
  summary: "Added auth/session validation tests."
  changed_files:
    - apps/api/tests/auth/session.test.ts
  commands_run:
    - npm test -- auth/session
  test_results:
    status: passed
    details: "12 tests passed."
  risks:
    - "Refresh-token edge cases may need integration coverage."
  artifacts:
    - path: apps/api/tests/auth/session.test.ts
  logs_path: ~/.jigga/sessions/subagent_sess_456/logs.md
```

---

## TypeScript Interface Example

```ts
export type SubagentBackend =
  | "codex_cli"
  | "codex_cloud"
  | "claude_code"
  | "local_agent_runtime";

export type SubagentMode = "plan" | "execute" | "review" | "research";

export interface SpawnSubagentInput {
  backend: SubagentBackend;
  mode: SubagentMode;
  parentAgentId: string;
  taskId: string;
  cwd: string;
  memoryScope: string;
  workOrder: {
    goal: string;
    instructions?: string;
    acceptanceCriteria?: string[];
  };
  permissions: PermissionPolicy;
  limits: {
    maxRuntimeMinutes?: number;
    maxFilesChanged?: number;
    maxOutputTokens?: number;
  };
  outputRequired: Array<
    | "summary"
    | "changed_files"
    | "commands_run"
    | "test_results"
    | "risks"
    | "artifacts"
  >;
}

export interface SpawnSubagentResult {
  sessionId: string;
  backend: SubagentBackend;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  summary?: string;
  changedFiles?: string[];
  commandsRun?: string[];
  testResults?: {
    status: "passed" | "failed" | "not_run";
    details?: string;
  };
  risks?: string[];
  artifacts?: Array<{ path: string; description?: string }>;
  logsPath?: string;
}
```

---

## Runtime Adapter Interface

Each backend should implement the same adapter contract.

```ts
export interface SubagentRuntimeAdapter {
  id: string;

  validate(input: SpawnSubagentInput): Promise<void>;

  start(input: SpawnSubagentInput): Promise<{
    sessionId: string;
    pid?: number;
    status: "running" | "queued";
  }>;

  getStatus(sessionId: string): Promise<SpawnSubagentResult>;

  cancel(sessionId: string): Promise<void>;
}
```

---

## Example Backend Adapters

### Codex CLI Adapter

The Codex CLI adapter launches a local Codex session in a scoped working directory.

```ts
export class CodexCliAdapter implements SubagentRuntimeAdapter {
  id = "codex_cli";

  async validate(input: SpawnSubagentInput) {
    assertAllowedBackend(input.backend);
    assertSafeWorkingDirectory(input.cwd);
    assertPermissions(input.permissions);
  }

  async start(input: SpawnSubagentInput) {
    const prompt = buildSubagentPrompt(input);

    const child = spawn("codex", ["exec", prompt], {
      cwd: input.cwd,
      env: buildRestrictedEnv(input),
      stdio: ["ignore", "pipe", "pipe"],
    });

    const sessionId = await sessionManager.register({
      parentAgentId: input.parentAgentId,
      taskId: input.taskId,
      backend: this.id,
      pid: child.pid,
    });

    pipeLogs(child, sessionId);
    enforceTimeout(child, input.limits.maxRuntimeMinutes);

    return {
      sessionId,
      pid: child.pid,
      status: "running" as const,
    };
  }

  async getStatus(sessionId: string) {
    return sessionManager.getResult(sessionId);
  }

  async cancel(sessionId: string) {
    return sessionManager.cancel(sessionId);
  }
}
```

### Claude Code Adapter

The Claude Code adapter follows the same contract.

```ts
export class ClaudeCodeAdapter implements SubagentRuntimeAdapter {
  id = "claude_code";

  async validate(input: SpawnSubagentInput) {
    assertAllowedBackend(input.backend);
    assertSafeWorkingDirectory(input.cwd);
    assertPermissions(input.permissions);
  }

  async start(input: SpawnSubagentInput) {
    const prompt = buildSubagentPrompt(input);

    const child = spawn("claude", ["--print", prompt], {
      cwd: input.cwd,
      env: buildRestrictedEnv(input),
      stdio: ["ignore", "pipe", "pipe"],
    });

    const sessionId = await sessionManager.register({
      parentAgentId: input.parentAgentId,
      taskId: input.taskId,
      backend: this.id,
      pid: child.pid,
    });

    pipeLogs(child, sessionId);
    enforceTimeout(child, input.limits.maxRuntimeMinutes);

    return {
      sessionId,
      pid: child.pid,
      status: "running" as const,
    };
  }

  async getStatus(sessionId: string) {
    return sessionManager.getResult(sessionId);
  }

  async cancel(sessionId: string) {
    return sessionManager.cancel(sessionId);
  }
}
```

---

## Parent Agent Delegation Policy

Delegation must be explicitly configured per agent.

```yaml
agents:
  engineer:
    role: primary_software_agent
    can_delegate: true
    delegation:
      mode: elastic
      max_parallel_subagents: 4
      max_depth: 1
      require_parent_review: true
      subagents_can_spawn_subagents: false
      approval:
        required_above_subagents: 3
        required_for_cloud_backends: true
      allowed_backends:
        - codex_cli
        - claude_code
      triggers:
        - context_pressure
        - task_complexity_high
        - parallelizable_work
        - specialized_skill_needed
```

---

## Global Delegation Policy

The system should also enforce global limits.

```yaml
delegation_policy:
  enabled: true
  max_global_subagents: 8
  max_subagents_per_parent: 4
  max_depth: 1
  default_runtime_minutes: 20
  require_parent_review: true
  subagents_can_spawn_subagents: false
  cloud_backends_require_approval: true
  default_memory_scope: task_context_only
```

---

## When Should an Agent Delegate?

Delegation is appropriate when one or more of these signals are present:

### 1. Context Pressure

The parent agent cannot hold all relevant context in one run.

Example:

- large codebase analysis
- many files to inspect
- multiple documents to summarize

### 2. Parallelizable Work

The task can be split into independent workstreams.

Example:

- one subagent writes tests
- one subagent reviews security
- one subagent updates docs

### 3. Specialized Skill Needed

A different backend or role is better suited for the task.

Example:

- Codex for code implementation
- Claude Code for repo exploration and explanation
- reviewer agent for critique

### 4. Runtime Limit Risk

The task may exceed the primary agent's configured runtime.

### 5. Workflow Step Expansion

A workflow step expands into multiple independent subtasks.

---

## When Should an Agent NOT Delegate?

Delegation is not appropriate when:

- the task is simple
- the work is highly sequential
- the task requires sensitive personal memory
- the agent lacks permission to delegate
- the subagent would need broad filesystem access
- the user has not approved required cloud execution
- the parent agent cannot evaluate the result

---

## Subagent Prompt / Work Order Template

Every spawned subagent should receive a strict work order.

```text
You are a temporary subagent working for parent agent: {{parentAgentId}}.

Goal:
{{goal}}

Instructions:
{{instructions}}

Scope:
- Working directory: {{cwd}}
- Allowed files: {{allowedFiles}}
- Denied files: {{deniedFiles}}

Memory Scope:
{{memoryScopeSummary}}

Rules:
- Do not access files outside scope.
- Do not modify denied files.
- Do not spawn additional agents.
- Do not persist memory directly.
- Report all commands run.
- Report all files changed.
- Stop when the goal is complete or blocked.

Required Output:
1. Summary
2. Changed files
3. Commands run
4. Test results
5. Risks/blockers
```

---

## Memory Rules for Subagents

Subagents should use borrowed context, not owned memory.

### Allowed

- task-specific context
- project summaries
- relevant docs
- relevant code snippets
- workflow instructions

### Not Allowed By Default

- full user memory
- private personal preferences
- unrelated project history
- credentials
- global raw transcripts

### Persistence Rule

Subagents return findings to the parent.

The parent decides what gets written to:

```text
~/.jigga/memory/raw/
~/.jigga/memory/summaries/
~/.jigga/memory/structured/
```

---

## Session Storage

Recommended structure:

```text
~/.jigga/
  sessions/
    subagents/
      subagent_sess_456/
        input.yaml
        work_order.md
        stdout.log
        stderr.log
        result.yaml
        artifacts/
```

---

## Build Plan

### Phase 1 — Local Subagent Tool

Build:

- `spawn_subagent` tool definition
- session manager
- local process launcher
- Codex CLI adapter
- Claude Code adapter
- logs and result files

Do not build:

- nested subagents
- autonomous swarms
- cloud execution by default

### Phase 2 — Delegation Planner

Build planner heuristics:

- split task into work orders
- select backend
- set memory scope
- assign file scope
- enforce limits

### Phase 3 — Result Aggregation

Build:

- collect results from multiple subagents
- summarize outputs
- detect conflicts
- require parent review
- write approved memory updates

### Phase 4 — Workflow Integration

Allow workflows to declare delegation.

```yaml
workflow: implement_feature

steps:
  - id: plan
    agent: architect
    action: create_plan

  - id: parallel_execution
    agent: engineer
    action: delegate
    delegation:
      max_parallel_subagents: 3
      work_orders:
        - backend: codex_cli
          goal: "Implement API changes"
        - backend: codex_cli
          goal: "Write tests"
        - backend: claude_code
          goal: "Review docs and architecture impact"

  - id: review
    agent: engineer
    action: aggregate_and_review
```

### Phase 5 — Approval + Cost Controls

Add:

- approval for cloud backends
- cost estimates
- max runtime
- max parallel sessions
- user-visible plan before execution

---

## Example Usage

### User Request

```text
Implement the new billing webhook flow and make sure it has tests and docs.
```

### Primary Agent Decision

```yaml
decision:
  type: delegate
  reason: "Task includes implementation, tests, docs, and review. Work is parallelizable."
  subagents:
    - backend: codex_cli
      goal: "Implement billing webhook handler"
    - backend: codex_cli
      goal: "Write billing webhook tests"
    - backend: claude_code
      goal: "Review architecture and docs impact"
```

### Execution

```text
engineer
  ├─ codex_cli subagent: implementation
  ├─ codex_cli subagent: tests
  └─ claude_code subagent: review/docs
```

### Aggregation

```yaml
aggregate_result:
  status: needs_parent_review
  completed:
    - implementation
    - tests
    - review
  conflicts:
    - "Tests assume webhook secret env var name BILLING_SECRET, implementation used BILLING_WEBHOOK_SECRET."
  recommended_next_action:
    - "Resolve env var naming mismatch."
```

---

## Safety Rules

### Required in v1

```yaml
safety:
  max_depth: 1
  require_parent_review: true
  subagents_can_spawn_subagents: false
  default_network: disabled
  default_memory_scope: task_context_only
  log_everything: true
```

### Strongly Recommended

- run subagents in a restricted working directory
- deny secrets and dotfiles by default
- require approval for cloud backends
- limit runtime
- limit parallelism
- require structured outputs
- keep raw logs for auditability

---

## Recommended Repository Modules

```text
src/
  tools/
    spawnSubagent.ts
  delegation/
    delegationPlanner.ts
    resultAggregator.ts
    delegationPolicy.ts
  runtimes/
    codexCliAdapter.ts
    claudeCodeAdapter.ts
    localAgentAdapter.ts
  sessions/
    sessionManager.ts
    sessionStore.ts
  policy/
    permissions.ts
    sandbox.ts
```

---

## Design Decision: Bounded Swarm, Not Open Swarm

JIGGA should support swarm-like behavior, but only in a bounded way.

Allowed:

```text
Primary Agent → Subagents
```

Not allowed in v1:

```text
Primary Agent → Subagent → Subagent → Subagent
```

This avoids runaway recursion, cost explosions, permission leakage, and confusing accountability.

---

## Final Definition

> JIGGA supports bounded swarm execution through elastic delegation. When a worker detects that a job is too large, specialized, or parallelizable, it can spawn temporary subagents through approved backends such as Codex or Claude Code. Each subagent receives a scoped work order, limited memory, explicit permissions, runtime limits, and required result reporting. The parent agent remains accountable for review, merge decisions, and memory persistence.

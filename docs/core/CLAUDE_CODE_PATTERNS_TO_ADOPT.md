# Claude Code Patterns Worth Adopting

This document records the Claude Code-inspired system patterns that JIGGA should adapt for a broader AI worker operating system.

## Concepts to Adopt

### 1. Project-Local AI Directory

Use `.jigga/` to store project settings, agents, skills, hooks, workflows, rules, and memory metadata.

### 2. Instruction Files

Use `JIGGA.md` for committed project instructions and `JIGGA.local.md` for private user-specific context.

### 3. Path-Scoped Rules

Allow repositories to define rules by path so agents behave differently in code, docs, content, deployment, and sensitive directories.

### 4. Hooks

Support lifecycle hooks around tool use, file edits, shell commands, task completion, and memory writes.

### 5. Subagents With Isolated Context

Allow primary agents to spawn bounded subagents while preventing context bloat and uncontrolled authority inheritance.

### 6. Permission Modes

Define explicit autonomy levels such as `plan_only`, `ask`, `accept_edits`, `autonomous`, and `locked_down`.

### 7. Permissions vs Sandboxing

Keep policy and enforcement separate:

```text
Permission policy = what an agent may attempt
Sandbox = what the process can actually access
```

### 8. Managed Settings Hierarchy

Support organization/user/project/agent/workflow configuration layers with safe precedence.

### 9. Context Inspection and Compaction

Expose commands to inspect active context and compact memory without losing critical decisions.

### 10. MCP-Style Extension Layer, Safely Wrapped

Support external tools through a capability registry rather than unconstrained tool loading.

## JIGGA Positioning

Claude Code is an agentic coding environment. JIGGA should borrow its best primitives but apply them to a local-first AI worker operating system that coordinates agents, teams, workflows, memory, and policy across many types of work.

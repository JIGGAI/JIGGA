# JIGGA Memory Model

## Goal

JIGGA memory should be local-first, file-first, persistent, inspectable, and scoped by role.

The system should not treat memory as one large prompt. It should treat memory as a layered knowledge system that can produce the right amount of context for the right agent at the right time.

## Core Idea

Agents are temporary. Memory persists.

This makes agents feel continuous without requiring every agent process to run forever.

## Memory Layers

### 1. Raw Memory

Unprocessed source material.

Examples:

- agent transcripts
- user notes
- task logs
- workflow outputs
- meeting summaries
- emails selected for memory
- calendar summaries
- project files selected for memory

Location:

```text
~/.jigga/memory/raw/
```

### 2. Structured Memory

Stable facts and explicit preferences.

Examples:

```yaml
user_preferences:
  communication_style: concise but complete
  prefers_local_first: true
  approval_required_for_autonomous_workflows: true
```

Location:

```text
~/.jigga/memory/structured/
```

### 3. Summary Memory

Compressed context for specific scopes.

Examples:

- manager_view.md
- project_view.md
- social_content_team.md
- daily_briefing_agent.md

Location:

```text
~/.jigga/memory/summaries/
```

### 4. Indexed Memory

Search indexes generated from raw and structured memory.

Examples:

- keyword index
- vector index
- metadata index

Location:

```text
~/.jigga/memory/indexes/
```

## Memory Scopes

Memory scopes determine what an agent can see.

Example scopes:

```yaml
memory_scopes:
  full_user:
    description: Complete user memory. Reserved for trusted core agents.
    includes:
      - raw
      - structured
      - summaries
      - indexes

  manager_view:
    description: User-level working summary for close collaborators.
    includes:
      - structured/preferences.yaml
      - summaries/user_goals.md
      - summaries/key_projects.md

  project_view:
    description: Project-specific context only.
    includes:
      - summaries/projects/current_project.md
      - structured/project_facts.yaml

  task_only:
    description: Only task-provided context and approved inputs.
    includes: []

  minimal:
    description: No persistent memory unless explicitly passed.
    includes: []
```

## Human Organization Analogy

Not every coworker knows the same amount about the user.

- Core assistant: deep memory
- Manager-like agent: broad summary
- Frequent collaborator: project and preference context
- Occasional contractor: task-only context

## Memory Access Rules

1. Default to least privilege.
2. Do not expose raw memory by default.
3. Prefer summaries unless raw context is necessary.
4. Require explicit permission for sensitive memory classes.
5. Log memory reads and writes.
6. Allow users to inspect and delete memory.

## Memory Write Types

Agents may propose memory writes in different categories.

```yaml
memory_write:
  type: preference
  confidence: high
  content: "User prefers workflows to require approval before recurring activation."
  source: conversation
  approval: optional
```

Possible types:

- preference
- fact
- project_state
- task_result
- workflow_result
- user_goal
- relationship
- recurring_pattern
- reminder
- summary

## Memory Compaction

Memory will grow over time. JIGGA needs compaction.

Compaction strategies:

- summarize completed tasks
- archive old raw logs
- update role-specific summaries
- preserve source references
- remove duplicate facts
- mark stale facts

## Retrieval Strategy

A good retrieval pipeline should combine:

1. scope filtering
2. metadata filtering
3. keyword search
4. vector search
5. summary loading
6. recency weighting
7. final context packing

## Example Agent Context Package

```yaml
agent_context:
  agent_id: daily_briefing_agent
  memory_scope: manager_view
  included:
    - summaries/user_preferences.md
    - summaries/calendar_patterns.md
    - structured/notification_preferences.yaml
  excluded:
    - raw/email_transcripts
    - secrets
    - unrelated_project_notes
```

## Memory Safety

Memory is one of the highest-risk parts of the system.

Risks:

- agents seeing too much
- accidental leakage to tools or models
- stale summaries causing bad decisions
- hidden memory drift
- sensitive data exposure

Mitigations:

- scoped memory
- explicit memory policies
- local-first storage
- user-inspectable files
- audit logs
- approval for sensitive persistent facts

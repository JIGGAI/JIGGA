# Context Inspection & Compaction

JIGGA needs explicit tools for inspecting and compacting context because persistent agents can accumulate too much history.

## Commands

```bash
jigga context inspect
jigga context inspect --agent engineer
jigga memory compact
jigga memory compact --scope project_view
jigga memory summarize --task task_123
```

## Context Sources

```text
1. System policy
2. Project instructions
3. Agent role
4. Workflow/task input
5. Scoped memory summaries
6. Retrieved raw memory snippets
7. Tool results
8. Recent conversation/session trace
```

## Compaction Strategy

Compaction should preserve:

- explicit user instructions
- active task goals
- decisions made
- constraints and permissions
- open questions
- changed files/artifacts
- risks and test results

Compaction may discard:

- repetitive tool logs
- low-value intermediate reasoning
- superseded drafts
- duplicate retrieval snippets

## Rule

Generated compaction summaries should never override human-authored instructions.

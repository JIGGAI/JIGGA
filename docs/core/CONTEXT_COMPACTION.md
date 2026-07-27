# Context Inspection & Compaction

JIGGA needs explicit tools for inspecting and compacting context because persistent agents can accumulate too much history.

## Commands

```bash
jigga memory inspect              # scopes + layers (shipped)
jigga memory compact [--dry-run]  # archive old raw/facts/tasks (shipped)
jigga memory search <query>       # scope-aware retrieval (shipped)
```

(Proposed, not yet built: per-agent context inspection — `jigga context
inspect --agent <id>` — and model-backed task summarization in compaction;
today compaction archives rather than summarizes.)

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

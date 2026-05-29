# Final MVP Runtime Notes

This document records the final MVP hardening pass after Phase 4. Existing planning docs remain unchanged.

## Added Hardening

- Actual agent task completions now emit normalized `agent.task_completed` audit events.
- Completed workflow runs now emit normalized `workflow.completed` audit events.
- Workflow inference now has real runtime event sources, not only manually inserted audit records.
- `jigga supervisor start` now exists as a bounded or continuous supervisor loop command.

## Supervisor Start

```bash
jigga supervisor start
jigga supervisor start --interval-seconds 30
jigga supervisor start --interval-seconds 0 --max-ticks 1
```

`--max-ticks` is intended for tests, demos, and controlled local runs. Without it, the supervisor loop continues until interrupted.

## Inference Event Sources

The workflow suggester scans audit logs for normalized repeated events:

- `agent.task_completed`
- `workflow.completed`

Suggested workflows remain conservative:

- `status: suggested`
- manual trigger by default
- generated step requires approval

## Verification

Final MVP hardening is covered by tests for:

- actual agent runs feeding workflow inference
- actual workflow completions feeding workflow inference
- bounded `supervisor start` CLI behavior

# Phase 4 Runtime Notes

This document records the Phase 4 MVP runtime behavior added after the original roadmap docs. It does not replace the existing architecture or roadmap documents.

## Scope

Phase 4 adds the first implementation slice for:

- permission model v1
- approval-aware workflow execution
- restricted safe process planning/execution
- Terraform-style runtime `plan` / `apply`
- heuristic workflow inference and approval-gated workflow application

## Commands

```bash
jigga validate
jigga plan
jigga apply --approve
jigga workflow suggest
jigga workflow apply <suggestion_id> --approve
```

## Permission Model v1

Policy decisions use three statuses:

- `allow` — action can proceed
- `ask` — action requires explicit approval
- `deny` — action is blocked

The first policy evaluator covers:

- workflow steps with `approval: required`
- missing required agents
- filesystem allow/deny rules
- shell modes and dangerous command patterns
- network mode checks

## Safe Process Runner

The safe process runner supports a dry-run-first contract:

- commands are checked against shell policy
- working directories are checked against filesystem policy
- dry runs produce planned artifacts without executing
- denied commands raise a policy error
- approved execution records stdout/stderr artifact paths

## Plan / Apply

`jigga plan` compares the current runtime configuration files against the last applied snapshot.

Tracked config areas:

- agents
- teams
- workflows
- memory

`jigga apply --approve` writes the current snapshot. Plans that include approval-sensitive changes return `needs_approval` unless approval is explicit.

Approval-sensitive examples:

- deleting config files
- workflows with triggers or approval-gated steps
- agents with shell or network permissions

## Workflow Inference MVP

`jigga workflow suggest` scans audit logs for repeated completed task/workflow events and emits suggested workflow drafts.

Suggested workflows are intentionally conservative:

- status is `suggested`
- trigger defaults to manual
- generated step approval is required
- `workflow apply` requires `--approve` to write the workflow YAML

## Test Coverage

Phase 4 tests cover:

- shell deny behavior and dangerous patterns
- filesystem allow/deny/ask behavior
- workflow `needs_approval` results
- safe process planning and policy denial
- runtime plan/apply snapshots
- config validation
- workflow suggestion and approval-gated application
- CLI smoke paths

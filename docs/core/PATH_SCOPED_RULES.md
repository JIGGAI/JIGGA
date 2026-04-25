# Path-Scoped Rules

Path-scoped rules let a project define different instructions for different parts of a workspace.

## Why

A single repository may contain APIs, frontend apps, docs, content calendars, tests, deployment files, and sensitive configuration. Agents should not apply one generic behavior everywhere.

## Example

```yaml
rules:
  - id: api_rules
    paths: ["apps/api/**"]
    instructions:
      - Use FastAPI conventions.
      - Add tests for every behavior change.
      - Run API tests before completion.

  - id: content_rules
    paths: ["content/**"]
    instructions:
      - Use the brand voice guide.
      - Produce LinkedIn, X, and newsletter variants when syndicating.

  - id: secrets_rules
    paths: [".env", "secrets/**"]
    permissions:
      read: false
      write: false
```

## Resolution

When an agent touches a path, JIGGA should merge:

```text
project instructions + matching path rules + agent role + workflow/task instructions
```

If rules conflict, the safest/most restrictive rule wins.

## Implementation Notes

- Path rules should support glob syntax.
- The matched rules should be visible in audit logs.
- The agent prompt should explicitly list the active path-scoped rules for its task.

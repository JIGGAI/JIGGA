# Hooks & Lifecycle Events

Hooks are deterministic actions that run at specific lifecycle points. They provide guardrails around agent behavior and keep workflows repeatable.

## Lifecycle Events

```yaml
hook_events:
  before_agent_run
  after_agent_run
  before_tool_use
  after_tool_use
  before_file_edit
  after_file_edit
  before_shell_command
  after_shell_command
  before_task_done
  after_memory_write
```

## Example Hooks

```yaml
hooks:
  before_tool_use:
    - id: block_secrets
      action: security.scan_tool_input

  after_file_edit:
    - id: format_code
      action: shell.run
      command: npm run format
      approval: not_required

  before_task_done:
    - id: require_tests
      action: validation.require_test_results
```

## Hook Types

Hooks may be:

1. Built-in JIGGA actions
2. Local scripts
3. HTTP endpoints
4. Agent prompts
5. Policy checks

## Safety Rules

- Hooks inherit the permission mode and policy of the current task unless explicitly restricted further.
- Hooks should be logged in audit traces.
- Hooks should be deterministic when possible.
- Hooks should not silently broaden permissions.

## Use Cases

- Block access to secrets
- Run formatters after edits
- Require tests before task completion
- Update memory indexes after file changes
- Notify the user after high-priority events
- Prevent external messages without approval

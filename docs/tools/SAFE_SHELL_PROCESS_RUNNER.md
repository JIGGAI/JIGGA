# Safe Shell & Process Runner

## Purpose

Agents will sometimes need to run local commands: tests, linters, build commands, package installs, or tool CLIs. Raw shell access is powerful and dangerous, so JIGGA needs a controlled process runner.

## Product Definition

The **Safe Shell Runner** executes commands under explicit workspace, environment, timeout, network, and permission constraints.

## Tool Definition

```yaml
tool: run_process
input:
  command: "npm test"
  cwd: ./apps/api
  timeout_seconds: 120
  network: deny
  env_policy: minimal
  background: false
```

## Runtime Contract

```ts
type RunProcessInput = {
  command: string
  cwd: string
  timeoutSeconds?: number
  background?: boolean
  pty?: boolean
  env?: Record<string, string>
  network?: "allow" | "deny" | "ask"
}
```

## Execution Modes

- `foreground`: return output after completion
- `background`: return session ID and stream logs later
- `pty`: for interactive CLI tools that require terminal behavior
- `sandboxed`: run in container/restricted OS context when available

## Policy Controls

```yaml
shell:
  mode: restricted
  allowed_commands:
    - npm
    - pnpm
    - pytest
    - git
  denied_patterns:
    - "rm -rf /"
    - "curl * | sh"
    - "cat ~/.ssh/*"
  require_approval:
    - package_install
    - network_access
    - elevated_permissions
```

## Output

```yaml
result:
  exit_code: 0
  stdout_path: ~/.jigga/sessions/sess_123/stdout.log
  stderr_path: ~/.jigga/sessions/sess_123/stderr.log
  summary: "Tests passed."
```

## Safety Rules

- Default to no elevated privileges.
- Default to workspace-local execution.
- Redact secrets from logs.
- Require approval for destructive commands.
- Require approval for shell commands from untrusted capability packs.
- Never pipe remote scripts into shell without explicit human approval.

## V1 Build Tasks

- Implement allow/deny command matcher.
- Add timeouts.
- Add background process sessions.
- Add log capture.
- Add approval gate for risky commands.

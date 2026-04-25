# Permission Modes

JIGGA should define permission modes that control how much autonomy an agent, team, workflow, or subagent has.

## Modes

```yaml
permission_modes:
  plan_only:
    description: Agent may inspect and propose, but cannot write or execute.

  ask:
    description: Agent must request approval before writes, shell, network, external messages, or recurring schedules.

  accept_edits:
    description: Agent may edit allowed files but must ask before shell/network/external actions.

  autonomous:
    description: Agent may act within explicit policy boundaries without per-action approval.

  locked_down:
    description: Agent may only read assigned task context and produce text output.
```

## Recommended Defaults

- User-facing personal admin agents: `ask`
- Coding agents in repo sandboxes: `accept_edits`
- Subagents: `locked_down` or `plan_only` unless explicitly elevated
- Workflow inference: `plan_only` until user approves
- Email/calendar write actions: `ask` by default

## Policy Precedence

Permission modes do not replace policy. They sit on top of hard allow/deny rules.

```text
mode says how approvals work
policy says what is possible at all
sandbox enforces the boundary
```

## Example

```yaml
agents:
  engineer:
    permission_mode: accept_edits
    permissions:
      filesystem:
        allow: ["src/**", "tests/**"]
        deny: [".env", "secrets/**"]
      shell:
        allow: ["npm test", "npm run lint"]
      network:
        mode: deny
```

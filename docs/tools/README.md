# Tool Capability Specs

This folder defines initial tool/capability designs for JIGGA. These are implementation-facing specs: each file explains the user value, architecture, config surface, lifecycle, safety boundaries, and starter APIs for one capability.

The goal is to give JIGGA an OpenClaw-like breadth of useful tools while keeping the design safer, more declarative, local-first, and memory-aware.

## Initial Capability Set

1. [Capability Registry & Skill Packs](CAPABILITY_REGISTRY_SKILL_PACKS.md)
2. [Elastic Delegation & Subagents](ELASTIC_DELEGATION_SUBAGENTS.md)
3. [Channel Gateway & Message Adapters](CHANNEL_GATEWAY_MESSAGE_ADAPTERS.md)
4. [Session Manager](SESSION_MANAGER.md)
5. [Scheduler, Heartbeat & Event Watchers](SCHEDULER_HEARTBEAT_EVENT_WATCHERS.md)
6. [Safe Shell & Process Runner](SAFE_SHELL_PROCESS_RUNNER.md)
7. [Filesystem Workspace Tooling](FILESYSTEM_WORKSPACE_TOOLING.md)
8. [Browser Automation](BROWSER_AUTOMATION.md)
9. [Notification Router](NOTIFICATION_ROUTER.md)
10. [Email & Calendar Connectors](EMAIL_CALENDAR_CONNECTORS.md)
11. [Workflow Inference](WORKFLOW_INFERENCE.md)
12. [Model Router & Fallbacks](MODEL_ROUTER_FALLBACKS.md)
13. [Skill Security Scanner](SKILL_SECURITY_SCANNER.md)
14. [Observability, Audit Logs & Traces](OBSERVABILITY_AUDIT_TRACES.md)

## Design Rule

Every capability should be treated as a bounded tool:

```text
Agent intent → policy check → scoped tool invocation → logged result → memory write decision
```

No tool should implicitly grant broad filesystem, network, shell, email, calendar, or memory access.

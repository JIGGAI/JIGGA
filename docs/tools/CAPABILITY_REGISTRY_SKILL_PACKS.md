# Capability Registry & Skill Packs

## Purpose

JIGGA needs a way to teach agents how to use tools, workflows, and domain-specific procedures without hardcoding every behavior into the agent runtime.

This capability defines a local-first registry of reusable capabilities, inspired by OpenClaw-style skill folders but with stricter security, versioning, validation, and memory-scoping.

## Product Definition

A **Capability Pack** is a folder that contains instructions, schemas, examples, scripts, references, and permission requirements for a reusable ability.

Examples:

- `social-post-writer`
- `github-pr-reviewer`
- `calendar-day-briefing`
- `browser-researcher`
- `codex-subagent-delegation`
- `pdf-summarizer`

## Folder Shape

```text
~/.jigga/capabilities/
  social-post-writer/
    CAPABILITY.md
    manifest.yaml
    examples/
    references/
    scripts/
    tests/
```

Project-specific packs may live in:

```text
<project>/.jigga/capabilities/
```

## Precedence

Highest to lowest:

1. Project capability packs
2. User-local capability packs
3. Bundled JIGGA capability packs
4. Remote registry packs, if enabled

## Manifest Example

```yaml
name: social-post-writer
version: 0.1.0
summary: Draft platform-specific posts from source material.
triggers:
  - "turn this into social posts"
  - "repurpose this article"
  - "syndicate this content"
requires:
  tools:
    - read_file
    - write_file
  memory_scopes:
    - brand_voice_summary
permissions:
  filesystem:
    read:
      - ./content/**
    write:
      - ./drafts/**
  network:
    mode: deny
risk_level: low
```

## Runtime Loading

The Supervisor loads capability metadata at startup and watches pack folders for changes.

```text
Supervisor boot
  ↓
scan capability paths
  ↓
validate manifests
  ↓
build capability index
  ↓
make capabilities available to eligible agents
```

## Agent Use

Agents do not automatically use every capability. The runtime filters capabilities based on:

- Agent role
- Task type
- Permission policy
- Installed dependencies
- Memory scope
- Workspace
- User approval state

## Required APIs

```ts
type CapabilityManifest = {
  name: string
  version: string
  summary: string
  triggers: string[]
  requires?: {
    tools?: string[]
    memory_scopes?: string[]
    binaries?: string[]
  }
  permissions: PermissionPolicy
  risk_level: "low" | "medium" | "high"
}
```

```ts
interface CapabilityRegistry {
  scan(): Promise<CapabilityManifest[]>
  validate(path: string): Promise<ValidationResult>
  resolveForAgent(agentId: string, task: Task): Promise<CapabilityManifest[]>
  install(pathOrUrl: string): Promise<InstallPlan>
}
```

## Plan / Apply Behavior

Installing or enabling a capability should produce a diff:

```text
Capability: github-pr-reviewer
Requires:
- filesystem read: ./src/**
- filesystem write: ./reviews/**
- network: github.com
- memory scope: project_view

Apply? [y/N]
```

## Safety Rules

- Never auto-enable remote capability packs.
- Reject symlinks inside packs.
- Validate YAML frontmatter and manifests.
- Flag shell scripts, curl/wget installers, credential access, crypto-wallet access, and obfuscated code.
- Require approval for capabilities that request shell, network, browser profile, email, calendar, or secrets access.

## V1 Build Tasks

- Implement local pack discovery.
- Implement manifest validation.
- Add `jigga capabilities list`.
- Add `jigga capabilities inspect <name>`.
- Add `jigga capabilities plan <path>`.
- Add `jigga capabilities apply <path>`.

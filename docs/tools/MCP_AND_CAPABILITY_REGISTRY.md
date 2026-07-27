# MCP & Capability Registry

JIGGA may eventually support MCP-style external tools and servers, but every external capability should be wrapped by the JIGGA capability registry.

## Goal

Make external tools usable by agents without giving them unchecked access to local files, credentials, networks, or private memory.

## Capability Descriptor

```yaml
capability:
  id: github_issues
  type: mcp_server
  description: Read and create GitHub issues.
  transport: stdio
  command: npx
  args: ["github-mcp-server"]
  permissions:
    network:
      allow_domains: ["github.com", "api.github.com"]
    secrets:
      required: ["GITHUB_TOKEN"]
    operations:
      read: true
      write: ask
```

## Registry Responsibilities

- Validate capability manifests
- Show permission diffs before install/enable
- Bind tools to agents/workflows explicitly
- Log every invocation
- Enforce network/filesystem/secret boundaries
- Allow disabling or revoking capabilities

## Install Flow

```bash
# (shipped syntax — the group is `capabilities`)
jigga capabilities validate github_issues.yaml
jigga capabilities install github_issues
```

## Safety Rules

- MCP tools should not be globally available by default.
- Capabilities must declare required secrets and network access.
- External tools should run in sandboxed processes where possible.
- Agents should receive only the tools assigned to their role/task.

# Filesystem Workspace Tooling

## Purpose

JIGGA is file-first. Agents need to read, write, search, patch, and organize files while respecting user-defined boundaries.

## Product Definition

The **Filesystem Tooling Layer** gives agents structured file tools instead of arbitrary filesystem access.

## Core Tools

```yaml
tools:
  - read_file
  - write_file
  - list_directory
  - search_files
  - apply_patch
  - create_directory
  - move_file
  - delete_file
```

## Workspace Policy

```yaml
filesystem:
  allow:
    - ~/Projects/current/**
    - ~/.jigga/memory/summaries/**
  deny:
    - ~/.ssh/**
    - ~/.gnupg/**
    - ~/Library/Keychains/**
    - ~/.env
  delete_requires_approval: true
```

## Tool Contract

```ts
interface FilesystemTools {
  readFile(path: string): Promise<string>
  writeFile(path: string, content: string): Promise<void>
  listDirectory(path: string): Promise<FileEntry[]>
  searchFiles(query: SearchQuery): Promise<SearchResult[]>
  applyPatch(patch: UnifiedDiff): Promise<PatchResult>
}
```

## Important Design Rule

Prefer structured file APIs over shell commands.

```text
Good: apply_patch(file, diff)
Riskier: run_process("python script_that_edits_files.py")
Riskiest: run_process("sed -i ...")
```

## Memory Integration

Filesystem events may be promoted into memory only when useful:

```text
file changed → watcher event → summary → memory write proposal
```

## V1 Build Tasks

- Implement path allow/deny checks.
- Implement safe read/write/list.
- Implement patch application.
- Add file change watcher.
- Add delete approval gate.

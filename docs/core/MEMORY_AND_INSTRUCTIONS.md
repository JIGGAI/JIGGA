# Memory & Instructions Files

JIGGA should support explicit project instruction files and local/private instruction files.

## Core Files

```text
JIGGA.md          # shared project/team instructions, committed to repo
JIGGA.local.md    # private user instructions, gitignored
.jigga/memory/    # generated summaries, indexes, facts, and traces
```

## Purpose

`JIGGA.md` is the human-readable operating manual for agents working in a project. It should explain project goals, conventions, commands, safety rules, and preferred workflows.

`JIGGA.local.md` lets an individual user add private context without leaking it into the repository.

## Suggested `JIGGA.md` Structure

```markdown
# JIGGA Project Instructions

## Project Purpose

## Architecture Overview

## Common Commands

## Coding Standards

## Testing Requirements

## Deployment Notes

## Agent Rules

## Do Not Touch
```

## Memory Layers

JIGGA memory should remain file-first:

```text
.jigga/memory/
  raw/             # transcripts, task logs, imported notes
  structured/      # facts, preferences, relationships, project state
  summaries/       # human-readable scoped summaries
  indexes/         # vector/keyword indexes derived from files
```

## Rule

Agents should load instructions and scoped memory separately:

```text
instructions = stable project rules
memory = evolving knowledge from work history
```

This prevents generated memory from silently overriding explicit human rules.

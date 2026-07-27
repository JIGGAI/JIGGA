# Skills

A **skill** is a reusable procedure an agent can run: a folder holding
`manifest.yaml` + `instructions.md`. Under the hood it's the `skill_pack`
capability type — installed, security-scanned, and first-use-approved like any
capability; invoking its action routes the instructions through the executing
agent's model. This doc covers what makes skills first-class.

## Progressive disclosure into the context pack

Two layers, driven by the manifest's `triggers:` list (whole-word,
case-insensitive matching against the task text):

- **"Your skills"** (stable): one line per skill the agent is granted —
  name, summary, actions, triggers. The agent always knows what procedures it
  owns without paying for their full text.
- **"Activated skill instructions"** (volatile, below the cache boundary):
  when a trigger matches the current task, the skill's full `instructions.md`
  (clipped) is injected — the agent has the procedure in front of it, and can
  still call the skill's action to execute it model-side.

Only **granted** skills surface (the skill's action must be in the agent's
`tools`/`permissions.tools.allow`) — surfacing an uncallable skill would
invite improvisation.

## CLI

```bash
jigga skills list [--json]     # installed skills + any pending approval
jigga skills show <name>       # manifest + instructions
jigga skills create <name>     # scaffold a user-local pack under ~/.jigga/capabilities/
```

`create` never auto-trusts: the new pack lands **pending** until
`jigga capabilities approve <pack>/manifest.yaml --approve` (the normal
first-use gate, hash-pinned against later edits).

## Authoring

```yaml
# ~/.jigga/capabilities/release-notes/manifest.yaml
name: release-notes
version: 0.1.0
type: skill_pack
summary: Draft release notes from merged PRs.
actions: [release_notes.run]
triggers: ["release notes", "changelog"]
risk_level: low
instructions: instructions.md
```

`instructions.md` is both the system prompt for `release_notes.run` and the
text injected on a trigger match. Keep it a procedure, not prose — the model
executes it.

## Follow-ups

- Per-role `SKILLS.md` notes layered like `TOOLS.md` (usage notes on top of
  the generated layer).
- Skill packs in recipes (a recipe ships its team's skills).
- Ties into W7 protocol boot (#63): a skill an agent *reads on demand* via
  filesystem tools instead of injection.

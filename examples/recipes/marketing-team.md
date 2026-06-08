---
id: marketing_team
name: Marketing Team
kind: team
version: 0.2.0
description: A marketing team that turns a product brief into reviewed, platform-ready launch copy.
purpose: Turn a product brief into reviewed launch copy.
memory_scope: task_only
routing:
  lead: strategy
default_workflows:
  - team_launch
# Ticket-board lanes — a CUSTOM vocabulary for this team (not the default
# backlog/working/review/done). `description` is injected into each agent's
# context so they know what a lane means; `gate` names the only member/role
# allowed to move a ticket OUT of that lane (the single enforced rule).
lanes:
  - id: brief
    description: Incoming product briefs to distill into a launch message.
  - id: drafting
    description: Copy actively being written.
  - id: review
    description: Drafted copy awaiting SEO/clarity review.
    gate: review        # only the review role moves a ticket out of review
  - id: published
    description: Approved, platform-ready copy.
# Extra workspace files written at scaffold time. `template` names an entry in
# `templates:`; `{{teamId}}`/`{{teamName}}` are substituted. createOnly (default)
# won't clobber edits on re-scaffold.
templates:
  charter: |
    # {{teamName}} — Charter

    Mission: turn a product brief into reviewed, platform-ready launch copy.
    Lead curates notes/plan.md and shared-context/priorities.md; others append
    to shared-context/agent-outputs/.
files:
  - path: notes/charter.md
    template: charter
    mode: createOnly
agents:
  - role: strategy
    id: marketing_lead
    required: true
    agent:
      name: Marketing Lead
      role: Distills a product into one sharp launch message and the single sharpest angle.
      description: Strategy lead for the marketing team example.
      memory_scope: task_only
      # profile:default uses whatever provider you've configured (dry-run until you
      # run `jigga model use chatgpt` / `jigga model login`). No tools — this agent
      # drafts text.
      model: profile:default
      tools: [memory.search, tickets.move, tickets.list]
      permissions:
        network:
          mode: ask
        shell:
          mode: deny
        tickets: move
  - role: drafting
    id: copywriter
    required: true
    agent:
      name: Copywriter
      role: Writes punchy launch copy for indie developers. No hashtags, no emoji.
      description: Drafting agent for the marketing team example.
      memory_scope: task_only
      model: profile:default
      tools: [memory.search, tickets.move, tickets.list]
      permissions:
        network:
          mode: ask
        shell:
          mode: deny
        tickets: move
  - role: review
    id: seo_editor
    required: true
    agent:
      name: SEO Editor
      role: Reviews copy for clarity, accuracy of claims, and keyword coverage.
      description: Review agent for the marketing team example.
      memory_scope: task_only
      model: profile:default
      tools: [memory.search, tickets.move, tickets.list]
      permissions:
        network:
          mode: ask
        shell:
          mode: deny
        tickets: move
workflows:
  - id: team_launch
    name: Team Launch
    purpose: Lead -> copywriter -> SEO editor, each step thinking on the model (draft_with_model).
    status: draft
    trigger:
      manual: true
    # Each step makes a real model call via the `draft_with_model` capability and the
    # prose is chained forward by named outputs. Edit the product in the first step.
    steps:
      - id: core_message
        agent: marketing_lead
        action: draft_with_model
        input:
          prompt: >-
            Product: a local-first OS for personal AI workers that run on your own
            machine. Distill the core launch message and the single sharpest angle in
            2-3 sentences.
        output: core_message.md
        approval: not_required
      - id: copy
        agent: copywriter
        action: draft_with_model
        input:
          prompt: "Write (a) one launch tweet under 200 characters and (b) a 3-sentence LinkedIn post."
          core_message: core_message.md
        output: copy.md
        approval: not_required
      - id: review
        agent: seo_editor
        action: draft_with_model
        input:
          prompt: "Give 3 short bullet notes on clarity, accuracy of claims, and keyword coverage."
          copy: copy.md
        output: review.md
        approval: not_required
    outputs:
      - core_message.md
      - copy.md
      - review.md
---

# Marketing Team recipe

Scaffolds a lead → copywriter → SEO-editor team with explicit agent ids and
the `team_launch` workflow. The team coordinates through its shared workspace.

```bash
jigga recipes scaffold marketing-team
jigga workflow run team_launch
```

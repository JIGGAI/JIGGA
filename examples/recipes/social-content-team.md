---
id: social_content_team
name: Social Content Team
kind: team
version: 0.2.0
description: Develop and syndicate content across platforms from source ideas or assets.
purpose: Develop and syndicate content across platforms from source ideas or assets.
memory_scope: content_team_view
routing:
  lead: strategy
  handoffs:
    - from: content_strategist
      to: linkedin_writer
      when: core_message_ready
    - from: linkedin_writer
      to: editor
      when: draft_ready
default_workflows:
  - social_content_syndication
policies:
  approvals:
    required_for:
      - external_publish
      - schedule_social_post
      - connect_new_account
agents:
  - role: strategy
    id: content_strategist
    required: true
    agent:
      name: Content Strategist
      role: Turns source ideas into content strategy and platform-specific direction.
      description: A content team agent used for social media development and syndication.
      model: gpt-5.5
      memory_scope: content_team_view
      tools:
        - extract_core_message
        - filesystem.read_file      # not `filesystem.read` — that names no capability
        - filesystem.write_file
        - memory.search
        - task.assign
      wake:
        events:
          - task.assigned.content_strategist
          - workflow.step.social_content_syndication.extract_core_message
        accepts_agent_requests: true
      permissions:
        memory:
          scope: content_team_view
        filesystem:
          allow:
            - ~/Projects/content
            - ~/.jigga/memory/summaries
          deny:
            - ~/.ssh
        network:
          mode: ask
        shell:
          mode: deny
      workflows:
        - social_content_syndication
      delegation:
        enabled: true
        allowed_backends:
          - dry_run
        max_parallel_subagents: 2
        max_depth: 1
  # Staffed content roles: each carries the tool its workflow step calls and
  # the permissions that tool's capability declares.
  - role: linkedin drafting
    id: linkedin_writer
    required: true
    agent:
      name: LinkedIn Writer
      role: Writes the LinkedIn variant of an approved core message.
      model: profile:default
      memory_scope: content_team_view
      tools:
        - draft_linkedin_post
        - memory.search
      permissions:
        # content-drafting declares these paths; an agent granted one of its
        # actions without them is offered a tool that fails on first use.
        filesystem:
          allow: ["~/Projects/content"]
          deny: ["~/.ssh"]
        network: {mode: ask}
        shell: {mode: deny}
      wake:
        events:
          - task.assigned.linkedin_writer
        accepts_agent_requests: true
  - role: thread drafting
    id: x_writer
    required: true
    agent:
      name: Thread Writer
      role: Writes the X/Twitter thread variant of an approved core message.
      model: profile:default
      memory_scope: content_team_view
      tools:
        - draft_thread
        - memory.search
      permissions:
        # content-drafting declares these paths; an agent granted one of its
        # actions without them is offered a tool that fails on first use.
        filesystem:
          allow: ["~/Projects/content"]
          deny: ["~/.ssh"]
        network: {mode: ask}
        shell: {mode: deny}
      wake:
        events:
          - task.assigned.x_writer
        accepts_agent_requests: true
  - role: newsletter drafting
    id: newsletter_writer
    required: false
    agent:
      name: Newsletter Writer
      role: Writes the newsletter blurb variant of an approved core message.
      model: profile:default
      memory_scope: content_team_view
      tools:
        - draft_blurb
        - memory.search
      permissions:
        # content-drafting declares these paths; an agent granted one of its
        # actions without them is offered a tool that fails on first use.
        filesystem:
          allow: ["~/Projects/content"]
          deny: ["~/.ssh"]
        network: {mode: ask}
        shell: {mode: deny}
      wake:
        events:
          - task.assigned.newsletter_writer
        accepts_agent_requests: true
  - role: editorial review
    id: editor
    required: true
    agent:
      name: Editor
      role: Reviews drafts for clarity, tone, and accuracy of claims before distribution.
      model: profile:default
      memory_scope: content_team_view
      tools:
        - review_tone_and_claims
        - memory.search
      permissions:
        # content-drafting declares these paths; an agent granted one of its
        # actions without them is offered a tool that fails on first use.
        filesystem:
          allow: ["~/Projects/content"]
          deny: ["~/.ssh"]
        network: {mode: ask}
        shell: {mode: deny}
      wake:
        events:
          - task.assigned.editor
        accepts_agent_requests: true
  - role: distribution preparation
    id: publisher
    required: false
    agent:
      name: Publisher
      role: Assembles the reviewed drafts into a distribution package.
      model: profile:default
      memory_scope: content_team_view
      tools:
        - prepare_distribution_package
        - memory.search
      permissions:
        # content-drafting declares these paths; an agent granted one of its
        # actions without them is offered a tool that fails on first use.
        filesystem:
          allow: ["~/Projects/content"]
          deny: ["~/.ssh"]
        network: {mode: ask}
        shell: {mode: deny}
      wake:
        events:
          - task.assigned.publisher
        accepts_agent_requests: true
workflows:
  - id: social_content_syndication
    name: Social Content Syndication
    purpose: Convert one source idea or asset into platform-specific content drafts.
    status: draft
    trigger:
      manual: true
    inputs:
      source_material:
        type: file_or_text
        required: true
    steps:
      - id: extract_core_message
        agent: content_strategist
        action: extract_core_message
        output: core_message.md
        approval: not_required
      - id: draft_linkedin
        agent: linkedin_writer
        action: draft_linkedin_post
        input:
          core_message: ${core_message.md}
        output: linkedin_post.md
        approval: not_required
      - id: draft_x_thread
        agent: x_writer
        action: draft_thread
        input:
          core_message: ${core_message.md}
        output: x_thread.md
        approval: not_required
      - id: draft_newsletter
        agent: newsletter_writer
        action: draft_blurb
        input:
          core_message: ${core_message.md}
        output: newsletter_blurb.md
        optional: true
        approval: not_required
      - id: editorial_review
        agent: editor
        action: review_tone_and_claims
        input:
          files:
            - linkedin_post.md
            - x_thread.md
            - newsletter_blurb.md
        output: editorial_notes.md
        approval: not_required
      - id: prepare_distribution
        agent: publisher
        action: prepare_distribution_package
        approval: required
    outputs:
      - linkedin_post.md
      - x_thread.md
      - newsletter_blurb.md
      - editorial_notes.md
    memory:
      write_summary: true
      write_raw: true
    permissions:
      required:
        - filesystem.read
        - filesystem.write
---

# Social Content Team

One strategist turns source material into a core message; platform writers,
an editor, and a publisher carry it through the `social_content_syndication`
workflow with enforced handoffs and an approval gate before distribution.

Most roles are membership-only — scaffold the team, then staff the writers
when you're ready.

```bash
jigga recipes scaffold social-content-team
```

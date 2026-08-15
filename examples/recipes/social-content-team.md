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
        - filesystem.read
        - filesystem.write
        - memory.search
        - task.create
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
  # Membership-only roles: referenced by the syndication workflow's steps,
  # staffed when you connect the relevant platforms.
  - role: linkedin drafting
    id: linkedin_writer
    required: true
  - role: thread drafting
    id: x_writer
    required: true
  - role: newsletter drafting
    id: newsletter_writer
    required: false
  - role: editorial review
    id: editor
    required: true
  - role: distribution preparation
    id: publisher
    required: false
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
          core_message: core_message.md
        output: linkedin_post.md
        approval: not_required
      - id: draft_x_thread
        agent: x_writer
        action: draft_thread
        input:
          core_message: core_message.md
        output: x_thread.md
        approval: not_required
      - id: draft_newsletter
        agent: newsletter_writer
        action: draft_blurb
        input:
          core_message: core_message.md
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

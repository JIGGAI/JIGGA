---
id: marketing-team
name: Marketing Team
kind: team
version: 0.1.0
description: A marketing team that turns a product brief into reviewed, platform-ready launch copy.
purpose: Turn a product brief into reviewed launch copy.
routing:
  lead: lead
agents:
  - role: lead
    name: "{{teamName}} Lead"
    description: Distills the product into a sharp launch message and the single sharpest angle.
    tools: [draft_with_model]
    # Scheduled work-loops are off by default (safe-idle). Set enabledByDefault
    # true (or add a wake.schedule to the scaffolded agent) to have the lead wake
    # on a cron and triage. `message` becomes the scheduled task's instruction.
    cronJobs:
      - id: lead-triage-loop
        schedule: "*/30 7-23 * * 1-5"
        enabledByDefault: false
        message: "Triage loop: review the workspace plan/priorities and any new tasks, then update notes/status.md."
  - role: copywriter
    name: Copywriter
    description: Writes punchy launch copy for indie developers; no hashtags, no emoji.
    tools: [draft_with_model]
  - role: editor
    name: SEO Editor
    description: Reviews copy for clarity, accuracy of claims, and keyword coverage.
    tools: [draft_with_model]
---

# Marketing Team recipe

Scaffolds a lead → copywriter → SEO-editor team. Each role becomes an agent
`{{teamId}}-<role>` and the team coordinates through its shared workspace.
Run the bundled `team_launch` workflow, or dispatch tasks to `{{teamId}}-lead`.

---
id: researcher
name: Researcher
kind: agent
version: 0.2.0
description: A standalone research agent that gathers and summarizes information on a topic.
agent:
  role: Gathers and summarizes information on a topic.
  model: profile:default
  tools: [summarize_relevant_context]
  permissions:
    network: {mode: ask}
    shell: {mode: deny}
  # An enabled scheduled work-loop: this solo agent wakes every weekday morning.
  # `message` becomes the task instruction the agent acts on.
  cronJobs:
    - id: morning-research
      schedule: "0 8 * * 1-5"
      enabledByDefault: true
      message: "Morning research loop: check for assigned research tasks and produce a short briefing in your workspace."
---

# Researcher (single-agent recipe)

`jigga recipes scaffold researcher --id my-researcher` creates one agent with a
daily work-loop. Team-less agents get their own per-agent workspace on first run.

# JIGGA Live Demo Walkthrough

What you can run **today** to see the runtime in action end-to-end. Every command in this doc was tested as-of the most recent commit. The whole walkthrough takes ~5 minutes and uses a throwaway runtime directory at `/tmp/jigga-demo`.

For context on what's still stubbed vs. real, see the **What's stubbed** section at the bottom and the [roadmap](../../docs/ROADMAP_TO_PRODUCTION.md) for the sequenced plan to fill those gaps.

---

## Prerequisites

- Python 3.11+
- The JIGGA repo checked out and installed (`pip install -e .` from the repo root)
- Linux or macOS for desktop notifications (Windows still says `unsupported-windows` cleanly)
- *Optional* — Codex CLI (`npm install -g @openai/codex`) and/or Claude Code CLI (`npm install -g @anthropic-ai/claude-code`) if you want to demo the external subagent backends

---

## Initial setup

```bash
# Throwaway runtime so the demo doesn't touch your real ~/.jigga
export JIGGA_HOME=/tmp/jigga-demo
rm -rf "$JIGGA_HOME"

# Create the runtime + copy example agents/teams/workflows
jigga --home "$JIGGA_HOME" init --examples

# See what got loaded
jigga --home "$JIGGA_HOME" state
```

You should see two agents (`content_strategist`, `daily_briefing_agent`), two teams, three workflows, four memory scopes, and zero tasks. The runtime directory at `$JIGGA_HOME` is now your fully-functioning JIGGA instance.

---

## Demo 1 — Real desktop notification (~30 seconds)

Drop a one-step workflow that pops a notification on your desktop, then run it.

```bash
cat > "$JIGGA_HOME/workflows/hello.yaml" <<'EOF'
id: hello
name: Hello from JIGGA
steps:
  - id: notify
    agent: daily_briefing_agent
    action: notifications.send
    input:
      title: "Hi from JIGGA"
      body: "Real notification, real runtime"
      urgency: normal
EOF

jigga --home "$JIGGA_HOME" workflow run hello
```

You should see an actual notification pop up. The `notify` step output in the JSON response shows `backend: "notify-send"` (Linux) or `"osascript"` (macOS) and `delivered: true`.

To force dry-run delivery without changing the workflow:

```bash
JIGGA_NOTIFICATION_MODE=dry_run jigga --home "$JIGGA_HOME" workflow run hello
```

The audit log records every send. Try `cat "$JIGGA_HOME/logs/events.jsonl" | grep notification` to see `notification.delivered` events.

---

## Demo 2 — MCP server end-to-end (~1 minute)

We ship a tiny working MCP server at `examples/capabilities/mcp-demo/` that speaks real JSON-RPC over stdio. Approve it, dispatch a workflow against it, get a real round-trip.

```bash
# Copy the demo pack into the runtime's user-local capabilities dir
cp -r examples/capabilities/mcp-demo "$JIGGA_HOME/capabilities/"

# First-use approval (this would have been blocked otherwise — try
# omitting --approve first to see the dry-run scan report)
jigga --home "$JIGGA_HOME" capabilities approve \
  "$JIGGA_HOME/capabilities/mcp-demo/manifest.yaml" --approve

# Confirm the capability now resolves
jigga --home "$JIGGA_HOME" capabilities inspect mcp-echo

# Dispatch a workflow that calls the demo server's demo.echo tool
cat > "$JIGGA_HOME/workflows/mcp.yaml" <<'EOF'
id: mcp
name: MCP echo demo
steps:
  - id: echo
    agent: daily_briefing_agent
    action: demo.echo
    input:
      message: "hello from JIGGA"
EOF

jigga --home "$JIGGA_HOME" workflow run mcp
```

In the response, `outputs.echo.source` is `capability.mcp_server` and `outputs.echo.result.content[0].text` contains your echoed argument. Real subprocess, real MCP handshake, real response.

---

## Demo 3 — Skill pack via the model router (~30 seconds)

A `skill_pack` capability loads instructions from disk and dispatches through the model router. We ship a demo pack that drafts a 3-point outline.

```bash
cp -r examples/capabilities/skill-demo "$JIGGA_HOME/capabilities/"
jigga --home "$JIGGA_HOME" capabilities approve \
  "$JIGGA_HOME/capabilities/skill-demo/manifest.yaml" --approve

cat > "$JIGGA_HOME/workflows/outline.yaml" <<'EOF'
id: outline
name: Outline demo
steps:
  - id: draft
    agent: daily_briefing_agent
    action: skill.draft_outline
    input:
      topic: "Launching JIGGA v1"
EOF

jigga --home "$JIGGA_HOME" workflow run outline
```

By default this uses the `dry_run` model provider (no API call, deterministic placeholder response). The output's `source` field is `capability.skill_pack` and `model_provider` is `dry_run`.

To dispatch against a real OpenAI-compatible provider:

```bash
# Edit $JIGGA_HOME/config.yaml and add under models.providers:
#   openai:
#     kind: openai_compatible
#     base_url: https://api.openai.com/v1
#     api_key_env: OPENAI_API_KEY
#     default_model: gpt-4o-mini
# And change models.profiles.default.primary to "openai".
# Then export your key:
export OPENAI_API_KEY=sk-...
jigga --home "$JIGGA_HOME" workflow run outline
```

---

## Demo 4 — Subagent delegation (dry-run, ~1 minute)

The full delegation machinery — work-order validation, policy checks, session persistence, audit events — works end-to-end against the `dry_run` backend. No external CLI needed.

```bash
# content_strategist ships with delegation enabled and a permission_mode of ask;
# spawn_subagent is risk_level=medium so we need autonomous mode to skip approval.
echo "" >> "$JIGGA_HOME/agents/content_strategist.yaml"
echo "permission_mode: autonomous" >> "$JIGGA_HOME/agents/content_strategist.yaml"

cat > "$JIGGA_HOME/workflows/delegate.yaml" <<'EOF'
id: delegate
name: Delegate demo
steps:
  - id: spawn
    agent: content_strategist
    action: spawn_subagent
    input:
      backend: dry_run
      mode: plan
      cwd: "~/Projects/content"
      work_order:
        goal: "Outline next week's content"
        instructions: "Three topic ideas, two sentences each."
      limits:
        max_runtime_minutes: 5
EOF

jigga --home "$JIGGA_HOME" workflow run delegate

# Inspect the persisted session
jigga --home "$JIGGA_HOME" sessions list
SESSION_ID=$(jigga --home "$JIGGA_HOME" sessions list | head -1 | awk '{print $1}')
jigga --home "$JIGGA_HOME" sessions inspect "$SESSION_ID"
```

The audit log carries `subagent.spawn.planned`, `subagent.spawn.started`, `subagent.spawn.completed`. The session JSON lives at `$JIGGA_HOME/sessions/<id>/session.json`.

---

## Demo 5 — External subagent backends with OAuth (~1 minute, optional)

If you have `codex` or `claude` installed, you can run the same subagent flow against real external CLIs. JIGGA wraps each CLI's own OAuth login flow so no API keys are needed.

```bash
# See which external backends are installed and on PATH
jigga auth status

# Authenticate (opens browser, attaches to your TTY for the OAuth flow)
jigga auth login codex_cli
# or
jigga auth login claude_code
```

Each CLI stores credentials under `~/.codex/` / `~/.claude/`. JIGGA's sandbox passes `HOME` through to the subagent subprocess so the upstream CLI finds its own creds without JIGGA ever touching them.

Once authenticated, flip the runtime gates and run a workflow against the real backend:

```bash
# Enable the global flag in config.yaml
python -c "
import yaml
from pathlib import Path
p = Path('$JIGGA_HOME/config.yaml')
data = yaml.safe_load(p.read_text())
data['delegation_policy']['codex_cli_enabled'] = True
data['delegation_policy']['allowed_backends'] = ['dry_run', 'codex_cli']
p.write_text(yaml.safe_dump(data, sort_keys=False))
"

# Add codex_cli to the agent's allowed_backends
python -c "
import yaml
from pathlib import Path
p = Path('$JIGGA_HOME/agents/content_strategist.yaml')
data = yaml.safe_load(p.read_text())
data['delegation']['allowed_backends'] = ['dry_run', 'codex_cli']
p.write_text(yaml.safe_dump(data, sort_keys=False))
"

# Re-run the delegation workflow with backend: codex_cli in the work order
# (edit $JIGGA_HOME/workflows/delegate.yaml and change `backend: dry_run` → `backend: codex_cli`)
jigga --home "$JIGGA_HOME" workflow run delegate
```

The Codex subprocess runs with a restricted environment (PATH/HOME/LANG/LC_ALL/TERM only — no API keys leak), in the agent-approved working directory, with a configurable timeout. Stdout/stderr are captured into the session log.

---

## Demo 6 — Terraform-style plan / apply (~30 seconds)

Modify a workflow, see the diff, approve, apply. This is the most novel UX in the runtime.

```bash
# Edit a workflow file (any change works — title, add a step, change a trigger)
sed -i 's/name: Morning Day Summary/name: Morning Day Summary (updated)/' \
  "$JIGGA_HOME/workflows/morning_day_summary.yaml"

# See what changed
jigga --home "$JIGGA_HOME" plan

# Activate it (workflow files with approval-gated steps require --approve)
jigga --home "$JIGGA_HOME" apply --approve
```

The plan output lists each created/updated/deleted config file and flags which changes require approval (workflow activation, agents with shell/network permissions, deletions). Without `--approve`, `apply` returns `needs_approval` rather than applying.

---

## Demo 7 — Workflow inference (~1 minute)

After running enough workflows, JIGGA notices repeated patterns and suggests a workflow YAML you can review and apply.

```bash
# Run the morning briefing a few times to generate audit signal
for i in 1 2 3; do
  jigga --home "$JIGGA_HOME" workflow run morning_day_summary > /dev/null
done

# Ask for suggestions
jigga --home "$JIGGA_HOME" workflow suggest
```

The suggester groups completed events into time-windowed sessions, detects repeated session shapes, and emits structured suggestion JSON. Apply a specific suggestion with `jigga workflow apply <suggestion_id> --approve`.

---

## Demo 8 — Supervisor loop (~30 seconds)

The always-on daemon that polls schedules, cron events, and pending tasks, then wakes the right agents. For a demo, run a bounded loop.

```bash
# Run 3 ticks, 2-second interval, then exit cleanly
jigga --home "$JIGGA_HOME" supervisor start --interval-seconds 2 --max-ticks 3
```

The result JSON shows `tick_count`, the events seen each tick, which agents got woken, which got throttled (the `max_wakes_per_agent_per_hour` guard is real). Send SIGTERM mid-loop and you'll get `status: interrupted, stopped_by_signal: 15` with a clean shutdown audit event.

For a real demo, leave it running and create a task in another shell:

```bash
# In a second terminal:
JIGGA_HOME=/tmp/jigga-demo jigga --home /tmp/jigga-demo task create \
  --title "Smoke test" --assignee daily_briefing_agent
```

The supervisor picks it up on the next tick and runs the agent.

---

## What's stubbed (so you know the edges)

This is the honest list of what's *not* real yet, so you don't oversell what the demo shows:

- **Calendar/email actions** (`calendar.list_events`, `calendar.get_event`, `email.search`) return hardcoded fake data. `morning_day_summary` "works" but doesn't read your real Google Calendar or inbox. Milestone A in the roadmap is where these become real.
- **No real channel inputs.** Everything is CLI-driven today. No Slack/webhook/email-watcher yet — Milestone B.
- **`social_content_syndication`** references `linkedin_writer`/`x_writer`/`editor` agents that aren't in the examples. The plan output correctly shows them as `blocked: Agent <name> is not configured.` — useful for demoing the planning UX, not for actually drafting content.
- **Model router defaults to `dry_run`.** Skill packs and agent task execution go through it; without configuring a real provider, you get deterministic placeholder responses. Adding an OpenAI-compatible provider is a 5-line `config.yaml` edit.

---

## Where to look next

- [`docs/ROADMAP_TO_PRODUCTION.md`](../../docs/ROADMAP_TO_PRODUCTION.md) — sequenced milestones A through G from current runtime to v1.0.
- [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) — the always-on supervisor + temporary agent runtime model.
- [`docs/MVP_ROADMAP.md`](../../docs/MVP_ROADMAP.md) — the original 4-week build plan (Phases 1–4 all merged).
- [`docs/SUBAGENT_RUNTIME_NOTES.md`](../../docs/SUBAGENT_RUNTIME_NOTES.md) — how subagent delegation is gated.
- [`docs/NOTIFICATION_ADAPTER_RUNTIME_NOTES.md`](../../docs/NOTIFICATION_ADAPTER_RUNTIME_NOTES.md) — how the notification handler routes (and why it doesn't go through the sandbox).
- [`jigga/runtime/sandbox.py`](../../jigga/runtime/sandbox.py) module docstring — the canonical statement of the subprocess routing rule.

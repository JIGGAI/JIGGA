from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from jigga.commands.init import init_runtime
from jigga.commands.onboard import run_onboarding
from jigga.commands.install import (
    install_capability,
    list_available_capabilities,
    maybe_prompt_after_init,
    uninstall_capability,
)
from jigga.commands.state import inspect_state
from jigga.core.paths import get_paths, project_capabilities_dir, resolve_project_root
from jigga.runtime.agent import run_agent
from jigga.runtime.auth import auth_status, run_external_login
from jigga.runtime.capabilities import CapabilityRegistry, load_capability_manifest, record_approval
from jigga.runtime.audit_query import format_event, query_events, tail_events
from jigga.runtime.audit_query import trace as trace_events
from jigga.runtime.cost import budget_status, cost_summary
from jigga.runtime.log_rotation import rotate_logs
from jigga.runtime.capability_scanner import scan_capability
from jigga.runtime.approvals import pending_approvals, resolve_and_requeue
from jigga.runtime.channel_listener import DEFAULT_LONG_POLL_SECONDS, channel_listen, enabled_channels
from jigga.runtime.channels import ensure_default_channel
from jigga.runtime.gog import (
    DEFAULT_SERVICES,
    gog_auth_status,
    keyring_password_path,
    run_gog_interactive,
)
from jigga.runtime.telegram import (
    allowed_chat_ids as telegram_allowed_chat_ids,
    bot_token_path as telegram_bot_token_path,
    load_bot_token as telegram_load_bot_token,
    load_offset as telegram_load_offset,
    poll_messages as telegram_poll_messages,
)
from jigga.runtime.google_calendar import (
    client_config_path,
    delete_tokens,
    load_client_config,
    load_tokens,
    run_oauth_flow,
    tokens_path,
)
from jigga.runtime.inference import apply_suggestion, suggest_workflows
from jigga.runtime.memory import inspect_memory
from jigga.runtime.memory_index import rebuild_index, search_memory
from jigga.runtime.compaction import compact_memory
from jigga.runtime.memory_proposals import apply_proposal, list_proposals
from jigga.runtime.model_router import build_task_model_request, call_model, load_model_config
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.plan_apply import apply_runtime, plan_runtime, validate_runtime_configs
from jigga.runtime.daemon import record_supervisor_start, supervisor_loop
from jigga.runtime.scheduler import serialize_events, due_events
from jigga.runtime.subagents import cancel_session, list_sessions, read_session
from jigga.runtime.supervisor import supervisor_tick
from jigga.runtime.tasks import create_task, list_tasks, set_task_state
from jigga.runtime.handoffs import fire_handoffs, read_decision_log
from jigga.runtime.team import run_team
from jigga.runtime.recipes import (
    add_agent_to_team,
    create_recipe_instance,
    find_recipe,
    installed_recipes,
    lint_recipe,
    list_recipe_files,
    list_recipes,
    load_recipe,
    recipe_summary,
    scaffold_agent,
    scaffold_team,
)
from jigga.runtime.term_select import Option, multi_select, select_one, supports_picker
from jigga.runtime.workspaces import scaffold_workspace, workspace_dir
from jigga.runtime.workflow import plan_workflow, run_workflow
from jigga.core.config import (
    default_permission_mode,
    load_agents,
    load_teams,
    load_workflows,
    resolve_default_agent,
)


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _set_model_provider(paths: Any, provider: str, model: str | None) -> None:
    """Make `provider` the default in config.yaml, creating its provider entry
    and pointing the default profile at it (preserves other config)."""
    config = read_yaml(paths.config)
    models = config.setdefault("models", {})
    providers = models.setdefault("providers", {})
    profiles = models.setdefault("profiles", {})
    if provider == "chatgpt":
        providers["chatgpt"] = {"kind": "chatgpt_oauth", "default_model": model or "gpt-5.5", "timeout_seconds": 120}
        # Subscription billing: $0 marginal, but keep tokens tracked in `jigga cost`.
        models.setdefault("pricing", {}).setdefault(model or "gpt-5.5", {"input_per_1k": 0.0, "output_per_1k": 0.0})
    elif provider == "dry_run":
        providers["dry_run"] = {"kind": "dry_run", "default_model": "dry-run"}
    models["defaults"] = {**(models.get("defaults") or {}), "provider": provider}
    profiles["default"] = {**(profiles.get("default") or {}), "primary": provider, "fallback": []}
    write_yaml(paths.config, config)


# Installable channels: channel name -> (optional capability to install, blurb).
# New adapters (Slack, iMessage) register here once their capability exists.
# A `None` capability means nothing to install — the channel is bundled and
# needs no auth (webchat: a local file the browser chat page reads/writes).
_CHANNEL_CATALOG: dict[str, tuple[str | None, str]] = {
    "telegram": ("telegram", "Telegram bot — poll inbound + reply"),
    "webchat": (None, "Browser chat (jiggaview Chat page) — local file-backed, no auth"),
}


def _channels_setup(paths: Any, *, prompt: Any = input, echo: Any = print,
                    preselected: str | None = None) -> None:
    """Interactive channel onboarding: pick a channel → run its guided install
    (auth + config + approval, reused from `capabilities install`) → set the
    activation mode → enable. Pluggable via `_CHANNEL_CATALOG`.

    `preselected` skips the picker — onboarding already asked which driver, and
    asking the same question twice in one flow reads as a bug."""
    names = sorted(_CHANNEL_CATALOG)
    if preselected in _CHANNEL_CATALOG:
        name = preselected
    elif supports_picker():
        picked = select_one("Set up a channel",
                            [Option(label=n, detail=_CHANNEL_CATALOG[n][1]) for n in names])
        name = names[picked] if picked is not None else None
    else:
        echo("Set up a channel:")
        for i, name in enumerate(names, 1):
            echo(f"  {i}) {name} — {_CHANNEL_CATALOG[name][1]}")
        choice = prompt("> ").strip()
        name = names[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(names) else (
            choice if choice in _CHANNEL_CATALOG else None
        )
    if name is None:
        echo("No channel selected.")
        return

    capability = _CHANNEL_CATALOG[name][0]
    # Reuse the channel's guided install: token/credentials + config + approval.
    # Bundled channels (capability=None) have nothing to install or authenticate.
    if capability is not None and install_capability(paths, capability, input_fn=prompt, print_fn=echo) != 0:
        return

    activation = [
        ("always", "every allowed message"),
        ("mention", "DMs always; groups only when @mentioned"),
        ("direct_message_only", "only 1:1 chats"),
        ("disabled", "polled but inert"),
    ]
    if supports_picker():
        picked = select_one("Activation mode — when should the bot respond?",
                            [Option(label=key, detail=blurb) for key, blurb in activation])
        mode = activation[picked][0] if picked is not None else "always"
    else:
        echo("\nActivation mode — when should the bot respond?")
        for i, (key, blurb) in enumerate(activation, 1):
            echo(f"  {i}) {key:20} ({blurb})")
        mode = {str(i): key for i, (key, _) in enumerate(activation, 1)}.get(
            prompt("> ").strip(), "always"
        )
    config = read_yaml(paths.config)
    entry = config.setdefault("channels", {}).setdefault(name, {})
    entry["enabled"] = True
    entry["activation"] = mode
    ensure_default_channel(config, name)
    write_yaml(paths.config, config)

    # Close the reply loop: the agent that messages route to can only act on the
    # channel if it holds the channel's tools. The channel capability is opt-in,
    # so the default agent (scaffolded from *bundled* caps) doesn't get them
    # automatically — grant the whole set here, or inbound messages create tasks
    # that complete silently.
    granted = _grant_channel_tools(paths, name, config)
    if granted:
        agent_id, items = granted
        echo(f"  Granted '{agent_id}' {name} access — " + "; ".join(items))

    echo(f"\n✓ {name} ready (activation={mode}). The supervisor polls it each tick — "
         "run `jigga supervisor run` (or install it as a service).")


def _grant_channel_tools(paths: Any, channel: str, config: dict,
                         agent_id: str | None = None) -> tuple[str, list[str]] | None:
    """Grant the channel capability's agent-facing actions (send/reply + any
    future ones — NEVER runtime-only ingest actions like poll, which belong to
    the supervisor) to `agent_id` when given, else the channel's routed agent —
    its configured `default_agent`, else the install's default agent — so it
    can reply on the channel. Persists the agent yaml. Returns (agent_id,
    newly_added_actions), or None if there's no agent to grant to or it
    already has them all."""
    routed = agent_id \
        or (config.get("channels", {}).get(channel, {}) or {}).get("default_agent") \
        or resolve_default_agent(paths.agents)
    if not routed:
        return None
    agent_path = paths.agents / f"{routed}.yaml"
    if not agent_path.exists():
        return None
    # Catalog entry with capability=None (bundled channel) → the capability is
    # named after the channel itself (webchat → bundled `webchat` capability).
    capability_name = _CHANNEL_CATALOG.get(channel, (channel,))[0] or channel
    registry = CapabilityRegistry.load(user_capabilities=paths.capabilities, approvals_dir=paths.policies)
    capability = registry.get(capability_name)
    actions = ([a for a in capability.actions if not capability.is_runtime_only(a)]
               if capability else [f"{channel}.send_message"])
    doc = read_yaml(agent_path)
    granted: list[str] = []

    # 1. Tools — the channel's full action set (poll + send + future).
    tools = list(doc.get("tools") or [])
    added = [a for a in actions if a not in tools]
    if added:
        tools.extend(added)
        doc["tools"] = tools
        granted.append("tools: " + ", ".join(added))

    # 2. Network egress — the channel API host the send tool must reach. Without
    #    this the call is granted but blocked by the network gate. Targeted
    #    allowlist entry, NOT a blanket `network: allow`.
    net = (capability.permissions or {}).get("network") if capability else None
    target = net.get("target") if isinstance(net, dict) else None
    if target:
        perms = doc.get("permissions") or {}
        netperm = perms.get("network")
        if not isinstance(netperm, dict):
            netperm = {"mode": netperm if isinstance(netperm, str) else "ask"}
        allow_list = list(netperm.get("allow") or [])
        if netperm.get("mode") not in {"allow", "*"} and target not in allow_list:
            allow_list.append(target)
            netperm["allow"] = allow_list
            perms["network"] = netperm
            doc["permissions"] = perms
            granted.append(f"network: {target}")

    if not granted:
        return None
    write_yaml(agent_path, doc)
    return routed, granted


def _model_setup(paths: Any, *, prompt: Any = input, echo: Any = print) -> None:
    """Interactive onboarding: pick a provider, then (for ChatGPT) authenticate."""
    from jigga.runtime.chatgpt_auth import login_state
    if supports_picker():
        picked = select_one("Select a model provider", [
            Option(label="ChatGPT subscription", detail="no API key, runs on your ChatGPT plan"),
            Option(label="Dry-run", detail="no model; canned responses"),
        ])
        if picked is None:
            echo("Skipped model setup. Run `jigga model setup` later.")
            return
        wants_dry_run = picked == 1
    else:
        echo("Select a model provider:")
        echo("  1) ChatGPT subscription  (no API key, runs on your ChatGPT plan)")
        echo("  2) Dry-run               (no model; canned responses)")
        wants_dry_run = prompt("> ").strip() in ("2", "dry_run", "dry-run")
    if wants_dry_run:
        _set_model_provider(paths, "dry_run", None)
        echo("Provider set to dry_run.")
        return
    _set_model_provider(paths, "chatgpt", None)
    if login_state(paths.home).get("logged_in"):
        echo("Provider set to chatgpt — an existing login was found. Done.")
        return
    login_methods = [
        ("browser", "open a URL, paste the return URL back"),
        ("device", "enter a short code at a URL — best for headless/remote"),
        ("skip", "use an existing `codex login` if present"),
    ]
    if supports_picker():
        picked = select_one("Authenticate your ChatGPT subscription",
                            [Option(label=key.title(), detail=blurb) for key, blurb in login_methods])
        choice = login_methods[picked][0] if picked is not None else "skip"
    else:
        echo("\nAuthenticate your ChatGPT subscription:")
        echo("  1) Browser    (open a URL, paste the return URL back)")
        echo("  2) Device code (enter a short code at a URL — best for headless/remote)")
        echo("  3) Skip       (use an existing `codex login` if present)")
        choice = prompt("> ").strip()
    from jigga.runtime.chatgpt_login import browser_login, device_login
    if choice in ("1", "browser"):
        browser_login(paths.home)
    elif choice in ("2", "device", "device-code"):
        device_login(paths.home)
    else:
        echo("Skipped login. JIGGA will use an existing credential if one is available.")
        return
    echo("\n✓ Logged in. Check with: jigga model status")


def _print_cost(rows: list[dict[str, Any]], total: dict[str, Any], budgets: dict[str, Any]) -> None:
    if not rows:
        print("No model calls recorded.")
        return
    print(f"{'agent':<24}{'calls':>7}{'in_tok':>9}{'out_tok':>9}{'cost':>10}  budget")
    for row in rows:
        budget = budgets.get(row["agent"])
        if budget:
            flag = {"warn": " ⚠", "exceeded": " ⛔"}.get(budget["state"], "")
            budget_col = f"${budget['spent_usd']:.2f}/${budget['limit_usd']:.2f} ({budget['fraction'] * 100:.0f}%){flag}"
        else:
            budget_col = "—"
        print(f"{row['agent']:<24}{row['calls']:>7}{row['input_tokens']:>9}{row['output_tokens']:>9}"
              f"{'$' + format(row['cost_usd'], '.4f'):>10}  {budget_col}")
    print(f"{'total':<24}{total['calls']:>7}{total['input_tokens']:>9}{total['output_tokens']:>9}"
          f"{'$' + format(total['cost_usd'], '.4f'):>10}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jigga", description="Local-first operating system for personal AI workers.")
    parser.add_argument("--home", type=Path, default=None, help="JIGGA home directory")
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help=(
            "Project root containing .jigga/ with project-local capabilities. "
            "Auto-detected by walking up from cwd if omitted; overridden by JIGGA_PROJECT env var."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create a local runtime directory")
    init.add_argument("--examples", action="store_true",
                      help="Also scaffold the bundled example recipes (personal admin, marketing, social teams)")
    init.add_argument(
        "--no-prompt",
        action="store_true",
        help="Skip the post-init setup/capability prompts (also auto-skipped when stdin is not a TTY)",
    )

    setup = sub.add_parser("setup", help="Set up your assistant: who it works with, its purpose, and the default agent")
    setup.add_argument("--overwrite", action="store_true", help="Overwrite an existing default agent / USER.md")

    onboard = sub.add_parser(
        "onboard",
        help="Guided end-to-end setup: init -> assistant -> model -> channel -> always-on service",
    )
    onboard.add_argument("--examples", action="store_true",
                         help="Offer the example recipes for selection (installs all with --non-interactive)")
    onboard.add_argument("--overwrite", action="store_true", help="Overwrite an existing default agent / USER.md")
    onboard.add_argument("--install-daemon", action="store_true",
                         help="Install the supervisor as an always-on user service (launchd/systemd) at the end")
    onboard.add_argument("--service-interval", type=float, default=60,
                         help="Supervisor poll interval for the installed service (seconds)")
    onboard.add_argument("--skip-model", action="store_true", help="Don't offer model setup")
    onboard.add_argument("--skip-channels", action="store_true", help="Don't offer channel setup")
    onboard.add_argument("--non-interactive", action="store_true",
                         help="No prompts: scaffold defaults, skip model/channel steps (use --install-daemon to still install the service)")

    state = sub.add_parser("state", help="Inspect local runtime state")
    state.add_argument("--json", action="store_true", dest="json_output")

    plugins_p = sub.add_parser(
        "plugins", help="Install and supervise app plugins (out-of-process sidecars, e.g. jiggaview)")
    plugins_sub = plugins_p.add_subparsers(dest="plugins_command", required=True)
    plugins_install = plugins_sub.add_parser("install", help="Install from a local dir or git URL (scan + approve + setup + service)")
    plugins_install.add_argument("source", help="Local directory or git URL containing a type: app manifest.yaml")
    plugins_install.add_argument("--no-service", action="store_true", help="Install without registering the always-on service")
    plugins_install.add_argument("--json", action="store_true", dest="json_output")
    plugins_list = plugins_sub.add_parser("list", help="Installed plugins + service state")
    plugins_list.add_argument("--json", action="store_true", dest="json_output")
    for verb, help_text in (("start", "Start (register/kickstart) a plugin's service"),
                            ("stop", "Stop a plugin's service (unit removed; reinstallable with start)"),
                            ("status", "Show a plugin's service state"),
                            ("uninstall", "Stop the service and remove the plugin directory")):
        sp = plugins_sub.add_parser(verb, help=help_text)
        sp.add_argument("name")
        sp.add_argument("--json", action="store_true", dest="json_output")

    config_p = sub.add_parser("config", help="Read and edit runtime config (~/.jigga/config.yaml) by dotted key")
    config_sub = config_p.add_subparsers(dest="config_command", required=True)
    config_get = config_sub.add_parser("get", help="Print a value (or the whole config)")
    config_get.add_argument("key", nargs="?", help="Dotted key, e.g. channels.default")
    config_get.add_argument("--json", action="store_true", dest="json_output")
    config_set = config_sub.add_parser("set", help="Set a value (JSON-coerced: true/42/[..]; else string)")
    config_set.add_argument("key")
    config_set.add_argument("value")
    config_set.add_argument("--json", action="store_true", dest="json_output")
    config_unset = config_sub.add_parser("unset", help="Remove a key")
    config_unset.add_argument("key")
    config_unset.add_argument("--json", action="store_true", dest="json_output")

    secrets_p = sub.add_parser("secrets", help="Manage named secrets via the broker (values never shown)")
    secrets_sub = secrets_p.add_subparsers(dest="secrets_command", required=True)
    secrets_set = secrets_sub.add_parser("set", help="Store a secret (value prompted, never passed as an argument)")
    secrets_set.add_argument("name")
    secrets_sub.add_parser("list", help="List secret NAMES (never values)")
    secrets_delete = secrets_sub.add_parser("delete", help="Delete a secret")
    secrets_delete.add_argument("name")
    secrets_migrate = secrets_sub.add_parser(
        "migrate", help="Encrypt all plaintext secrets at rest (needs JIGGA_SECRETS_PASSPHRASE)")
    secrets_migrate.add_argument("--to", choices=["encrypted-file"], required=True)

    telemetry_p = sub.add_parser("telemetry", help="Opt-in anonymous telemetry (default off; `report` shows the exact payload)")
    telemetry_sub = telemetry_p.add_subparsers(dest="telemetry_command", required=True)
    for verb, h in (("on", "Enable"), ("off", "Disable"), ("status", "Enabled? last sent?"),
                    ("report", "Print the EXACT payload a send would transmit")):
        telemetry_sub.add_parser(verb, help=h)

    backup = sub.add_parser("backup", help="Back up / restore the runtime home (~/.jigga)")
    backup_sub = backup.add_subparsers(dest="backup_command", required=True)
    backup_create = backup_sub.add_parser(
        "create", help="Archive the home (secrets excluded by default) to a .tar.gz")
    backup_create.add_argument("--output", help="Archive path (default: ./jigga-backup-<ts>.tar.gz)")
    backup_create.add_argument("--include-secrets", action="store_true",
                               help="Also archive secrets/ (credentials!) — off by default")
    backup_create.add_argument("--encrypt", choices=["age", "gpg"],
                               help="Pipe through age/gpg (must be installed; passphrase prompts on the tty)")
    backup_create.add_argument("--recipient", help="age recipient (with --encrypt age; else passphrase mode)")
    backup_inspect = backup_sub.add_parser("inspect", help="Show a backup archive's manifest")
    backup_inspect.add_argument("archive")
    backup_restore = backup_sub.add_parser("restore", help="Restore an archive into a runtime home")
    backup_restore.add_argument("archive")
    backup_restore.add_argument("--force", action="store_true",
                                help="Non-empty target: move it aside (<home>.pre-restore-<ts>) first")

    update_p = sub.add_parser(
        "update", help="Reconcile this runtime with the current code: recipes, config keys, service")
    update_p.add_argument("--apply", action="store_true",
                          help="Apply without prompting (for scripts/automation)")
    update_p.add_argument("--dry-run", action="store_true",
                          help="Show the plan only; never prompt or apply")
    update_p.add_argument("--json", action="store_true", dest="json_output")

    doctor = sub.add_parser("doctor", help="Health check: runtime, config, model, channels, backends, service")
    doctor.add_argument("--json", action="store_true", dest="json_output", help="Machine-readable output")
    doctor.add_argument("--fix", action="store_true",
                        help="Apply safe auto-fixes (repair runtime layout, install/start the service), then re-check")
    doctor.add_argument("--no-probe", action="store_false", dest="probe",
                        help="Skip the live model request; report the provider as configured-but-unverified")
    doctor.add_argument("--probe", action="store_true", dest="probe", default=True,
                        help="Send one real request through the configured model provider (default)")

    memory = sub.add_parser("memory", help="Inspect memory scopes and layers")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_sub.add_parser("inspect", help="Inspect configured memory scopes")
    memory_search = memory_sub.add_parser("search", help="Keyword search over memory (scope-aware)")
    memory_search.add_argument("query")
    memory_search.add_argument("--scope")
    memory_search.add_argument("--team", help="Restrict to global memory + this team's/roles' memory")
    memory_search.add_argument("--limit", type=int, default=10)
    memory_search.add_argument("--rebuild", action="store_true", help="Force a fresh index first")
    memory_search.add_argument("--json", action="store_true", dest="json_output")
    memory_sub.add_parser("reindex", help="Rebuild the memory search index")
    memory_compact = memory_sub.add_parser("compact", help="Compact memory now (archive old raw / stale facts / finished tasks)")
    memory_compact.add_argument("--dry-run", action="store_true", dest="dry_run")
    memory_compact.add_argument("--json", action="store_true", dest="json_output")
    memory_proposals = memory_sub.add_parser("proposals", help="List pending memory write-proposals (sensitive facts awaiting approval)")
    memory_proposals.add_argument("--team")
    memory_proposals.add_argument("--json", action="store_true", dest="json_output")
    memory_approve = memory_sub.add_parser("approve", help="Approve a memory proposal (commit it to team memory)")
    memory_approve.add_argument("proposal_id")
    memory_approve.add_argument("--team")
    memory_reject = memory_sub.add_parser("reject", help="Reject a memory proposal")
    memory_reject.add_argument("proposal_id")
    memory_reject.add_argument("--team")

    skills = sub.add_parser("skills", help="List, inspect, and create skills (skill_pack capabilities)")
    skills_sub = skills.add_subparsers(dest="skills_command", required=True)
    skills_list = skills_sub.add_parser("list", help="Installed skills with triggers and actions")
    skills_list.add_argument("--json", action="store_true", dest="json_output")
    skills_show = skills_sub.add_parser("show", help="A skill's manifest + its instructions")
    skills_show.add_argument("name")
    skills_create = skills_sub.add_parser(
        "create", help="Scaffold a user-local skill pack (approve it with `jigga capabilities approve`)")
    skills_create.add_argument("name")
    skills_create.add_argument("--summary")

    workflow = sub.add_parser("workflow", help="Plan and run workflows")
    workflow_sub = workflow.add_subparsers(dest="workflow_command", required=True)
    workflow_plan = workflow_sub.add_parser("plan")
    workflow_plan.add_argument("workflow_id")
    workflow_plan.add_argument("--json", action="store_true", dest="json_output")
    workflow_run = workflow_sub.add_parser("run")
    workflow_run.add_argument("workflow_id")
    workflow_suggest = workflow_sub.add_parser("suggest")
    workflow_suggest.add_argument("--min-count", type=int, default=2)
    workflow_suggestions = workflow_sub.add_parser(
        "suggestions", help="Open workflow suggestions (with whether each is already created)")
    workflow_suggestions.add_argument("--min-count", type=int, default=2)
    workflow_suggestions.add_argument("--json", action="store_true", dest="json_output")
    workflow_list = workflow_sub.add_parser("list", help="List installed workflows")
    workflow_list.add_argument("--json", action="store_true", dest="json_output")
    workflow_apply = workflow_sub.add_parser("apply")
    workflow_apply.add_argument("suggestion_id")
    workflow_apply.add_argument("--approve", action="store_true")
    workflow_runs = workflow_sub.add_parser("runs", help="List v2 (DAG) workflow runs and their node states")
    workflow_runs.add_argument("workflow_id", nargs="?")
    workflow_runs.add_argument("--active", action="store_true", help="Only running / awaiting-approval runs")
    workflow_runs.add_argument("--json", action="store_true", dest="json_output")
    workflow_resume = workflow_sub.add_parser(
        "resume", help="Advance a v2 run now (e.g. right after `jigga approvals approve <code>`)")
    workflow_resume.add_argument("run_id")

    plan = sub.add_parser("plan", help="Plan runtime config changes")
    plan.add_argument("--json", action="store_true", dest="json_output")

    apply_cmd = sub.add_parser("apply", help="Apply runtime config snapshot")
    apply_cmd.add_argument("--approve", action="store_true")

    validate = sub.add_parser("validate", help="Validate runtime configs")
    validate.add_argument("--json", action="store_true", dest="json_output")

    capabilities = sub.add_parser("capabilities", help="Inspect capability registry")
    capabilities_sub = capabilities.add_subparsers(dest="capabilities_command", required=True)
    capabilities_sub.add_parser("list", help="List registered capabilities")
    capabilities_sub.add_parser("pending", help="List user-local capabilities awaiting approval")
    capability_inspect = capabilities_sub.add_parser("inspect", help="Inspect a registered capability")
    capability_inspect.add_argument("name")
    capability_validate = capabilities_sub.add_parser("validate", help="Validate a capability manifest")
    capability_validate.add_argument("path", type=Path)
    capability_approve = capabilities_sub.add_parser(
        "approve", help="Record a first-use approval for a user-local capability manifest"
    )
    capability_approve.add_argument("path", type=Path)
    capability_approve.add_argument("--approve", action="store_true", dest="confirm")
    capability_install = capabilities_sub.add_parser(
        "install",
        help="Install a first-party optional capability (interactive menu when no name)",
    )
    capability_install.add_argument(
        "name", nargs="?", help="Capability to install (e.g. google-calendar)"
    )
    capability_uninstall = capabilities_sub.add_parser(
        "uninstall", help="Remove a previously-installed optional capability"
    )
    capability_uninstall.add_argument("name")
    capabilities_sub.add_parser(
        "list-available", help="List first-party optional capabilities available to install"
    )

    auth = sub.add_parser("auth", help="Authenticate external subagent CLI backends (codex, claude)")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_sub.add_parser("status", help="Show which external CLI backends are installed and on PATH")
    auth_login = auth_sub.add_parser(
        "login", help="Run the upstream `<cli> login` flow for an external backend"
    )
    auth_login.add_argument("backend", help="Backend to authenticate (codex_cli, claude_code)")

    calendar = sub.add_parser(
        "calendar", help="Google Calendar status / login / logout (requires capability install first)"
    )
    calendar_sub = calendar.add_subparsers(dest="calendar_command", required=True)
    calendar_sub.add_parser("status", help="Show connection state for Google Calendar")
    calendar_sub.add_parser("login", help="Re-run OAuth login (refresh expired/revoked tokens)")
    calendar_sub.add_parser("logout", help="Delete stored Google Calendar tokens")

    gog = sub.add_parser(
        "gog", help="gog (Google Workspace) status / login / logout (requires capability install first)"
    )
    gog_sub = gog.add_subparsers(dest="gog_command", required=True)
    gog_sub.add_parser("status", help="Show gogcli install + auth state")
    gog_login = gog_sub.add_parser("login", help="Run/redo gog OAuth (gog auth add)")
    gog_login.add_argument("email", help="Google account email to authenticate")
    gog_login.add_argument(
        "--services",
        default=None,
        help="Comma-separated services to authorize (default: gmail,calendar,drive)",
    )
    gog_sub.add_parser("logout", help="Remove stored gog keyring password (does not revoke Google access)")

    telegram = sub.add_parser(
        "telegram", help="Telegram channel status / discover / logout (requires capability install first)"
    )
    telegram_sub = telegram.add_subparsers(dest="telegram_command", required=True)
    telegram_sub.add_parser("status", help="Show Telegram bot token + allowlist + offset state")
    telegram_sub.add_parser("discover", help="Poll once bypassing the allowlist to find your chat ID")
    telegram_sub.add_parser("logout", help="Delete the stored Telegram bot token")

    channels = sub.add_parser("channels", help="Channel listener (long-poll inbound -> tasks -> agents)")
    channels_sub = channels.add_subparsers(dest="channels_command", required=True)
    channels_sub.add_parser("status", help="List enabled channels and their config")
    channels_sub.add_parser("setup", help="Interactive: pick a channel, authenticate, set activation, enable")

    mailbox = sub.add_parser("mailbox", help="File-backed agent mailbox: send/list durable messages (W6)")
    mailbox_sub = mailbox.add_subparsers(dest="mailbox_command", required=True)
    mailbox_send = mailbox_sub.add_parser("send", help="Send a message to an agent's inbox (read on its next wake)")
    mailbox_send.add_argument("to", help="Recipient agent id")
    mailbox_send.add_argument("--body", required=True)
    mailbox_send.add_argument("--subject")
    mailbox_send.add_argument("--sender", default="human", help="Sender label (default: human)")
    mailbox_send.add_argument("--json", action="store_true", dest="json_output")
    mailbox_list = mailbox_sub.add_parser("list", help="List an agent's inbox")
    mailbox_list.add_argument("member", help="Agent id whose inbox to list")
    mailbox_list.add_argument("--unread", action="store_true", help="Unread only")
    mailbox_list.add_argument("--json", action="store_true", dest="json_output")

    approvals = sub.add_parser("approvals", help="Review and resolve pending action approvals")
    approvals_sub = approvals.add_subparsers(dest="approvals_command", required=True)
    approvals_list = approvals_sub.add_parser("list", help="List pending approvals")
    approvals_list.add_argument("--json", action="store_true", dest="json_output")
    approve_cmd = approvals_sub.add_parser("approve", help="Approve a pending action by code")
    approve_cmd.add_argument("code")
    deny_cmd = approvals_sub.add_parser("deny", help="Deny a pending action by code")
    deny_cmd.add_argument("code")
    channels_listen = channels_sub.add_parser("listen", help="Run the long-poll channel listener")
    channels_listen.add_argument("--long-poll-seconds", type=int, default=30)
    channels_listen.add_argument("--max-cycles", type=int, default=None, help="Stop after N cycles (tests/demos)")
    channels_listen.add_argument(
        "--no-process", action="store_true",
        help="Only enqueue messages as tasks; don't run agents (let the supervisor handle them)",
    )

    webchat = sub.add_parser(
        "webchat", help="Browser chat channel (jiggaview Chat page) — file-backed, local-only"
    )
    webchat_sub = webchat.add_subparsers(dest="webchat_command", required=True)
    webchat_send = webchat_sub.add_parser(
        "send", help="Send a message into webchat (first use enables the channel + grants the reply tool)"
    )
    webchat_send.add_argument("--text", required=True, help="Message text")
    webchat_send.add_argument(
        "--agent", default=None,
        help="Address a specific agent instead of the channel default (its thread = its id unless --conversation)",
    )
    webchat_send.add_argument("--conversation", default=None,
                              help="Conversation id (default: web, or --agent's id)")
    webchat_send.add_argument("--sender", default="you", help="Sender label (default: you)")
    webchat_send.add_argument(
        "--wait", action="store_true",
        help="Ingest inline and return the agent's reply in this invocation (synchronous chat UX)",
    )
    webchat_send.add_argument("--json", action="store_true", dest="json_output")
    webchat_history = webchat_sub.add_parser(
        "history", help="The merged conversation (your messages + agent replies), chronological"
    )
    webchat_history.add_argument("--conversation", default=None, help="Conversation id (default: web)")
    webchat_history.add_argument("--limit", type=int, default=200)
    webchat_history.add_argument("--json", action="store_true", dest="json_output")
    webchat_convs = webchat_sub.add_parser(
        "conversations", help="List every conversation thread (newest activity first)"
    )
    webchat_convs.add_argument("--json", action="store_true", dest="json_output")

    logs = sub.add_parser("logs", help="Tail the audit log")
    logs_sub = logs.add_subparsers(dest="logs_command", required=True)
    logs_tail = logs_sub.add_parser("tail", help="Show the most recent audit events")
    logs_tail.add_argument("-n", "--count", type=int, default=20)
    logs_tail.add_argument("--json", action="store_true", dest="json_output")
    logs_rotate = logs_sub.add_parser("rotate", help="Force a log rollover + retention prune now")
    logs_rotate.add_argument("--json", action="store_true", dest="json_output")

    audit = sub.add_parser("audit", help="Query the audit log with filters")
    audit.add_argument("--agent", help="Filter to an agent id")
    audit.add_argument("--type", dest="type_filter", help="Filter by event type (exact or family prefix)")
    audit.add_argument("--since", help="Only events newer than e.g. 30m / 24h / 7d or an ISO timestamp")
    audit.add_argument("--status", help="Filter by status (ok / error / ask / ...)")
    audit.add_argument("-n", "--count", type=int, default=None, help="Keep only the most recent N matches")
    audit.add_argument("--json", action="store_true", dest="json_output")

    trace = sub.add_parser("trace", help="Show events correlated to an id (run/task/session/event id)")
    trace.add_argument("identifier")
    trace.add_argument("--json", action="store_true", dest="json_output")

    cost = sub.add_parser("cost", help="Per-agent model cost rollup and budget status")
    cost.add_argument("--agent", help="Show only this agent")
    cost.add_argument("--since", help="Only calls newer than e.g. 24h / 7d / 30d or an ISO timestamp")
    cost.add_argument("--json", action="store_true", dest="json_output")

    sessions = sub.add_parser("sessions", help="Inspect subagent sessions")
    sessions_sub = sessions.add_subparsers(dest="sessions_command", required=True)
    sessions_list = sessions_sub.add_parser("list")
    sessions_list.add_argument("--json", action="store_true", dest="json_output")
    sessions_inspect = sessions_sub.add_parser("inspect")
    sessions_inspect.add_argument("session_id")
    sessions_cancel = sessions_sub.add_parser("cancel")
    sessions_cancel.add_argument("session_id")

    scheduler = sub.add_parser("scheduler", help="Inspect scheduler due events")
    scheduler_sub = scheduler.add_subparsers(dest="scheduler_command", required=True)
    scheduler_due = scheduler_sub.add_parser("due", help="List due events for the current time")
    scheduler_due.add_argument("--at", help="Evaluate due events at an ISO timestamp")

    agents_p = sub.add_parser("agents", help="List configured agents")
    agents_sub = agents_p.add_subparsers(dest="agents_command", required=True)
    agents_list = agents_sub.add_parser("list", help="List agents (id, role, model, team, default)")
    agents_list.add_argument("--json", action="store_true", dest="json_output")
    agents_get = agents_sub.add_parser("get", help="Read an agent's yaml (or one dotted key)")
    agents_get.add_argument("agent_id")
    agents_get.add_argument("key", nargs="?")
    agents_get.add_argument("--json", action="store_true", dest="json_output")
    agents_set = agents_sub.add_parser("set", help="Set a dotted key in an agent's yaml (validated, audited)")
    agents_set.add_argument("agent_id")
    agents_set.add_argument("key")
    agents_set.add_argument("value")
    agents_set.add_argument("--recipe", action="store_true", dest="via_recipe",
                            help="Recipe-first: write the change into the agent's definition in the "
                                 "team recipe (user-dir copy) and regenerate the yaml from it")
    agents_set.add_argument("--json", action="store_true", dest="json_output")
    for verb, help_text in (("disable", "Supervisor stops waking this agent (reversible; tasks stay pending)"),
                            ("enable", "Re-enable a disabled agent")):
        sp = agents_sub.add_parser(verb, help=help_text)
        sp.add_argument("agent_id")
        sp.add_argument("--json", action="store_true", dest="json_output")
    agents_delete = agents_sub.add_parser("delete", help="Delete an agent (backed up; recipe member de-staffed)")
    agents_delete.add_argument("agent_id")
    agents_delete.add_argument("--json", action="store_true", dest="json_output")
    agents_files = agents_sub.add_parser("files", help="List an agent's identity files (required/optional/missing)")
    agents_files.add_argument("agent_id")
    agents_files.add_argument("--json", action="store_true", dest="json_output")
    agents_file = agents_sub.add_parser("file", help="Read/write one identity file")
    agents_file.add_argument("verb", choices=["get", "set"])
    agents_file.add_argument("agent_id")
    agents_file.add_argument("name", help="File name, e.g. SOUL.md")
    agents_file.add_argument("--content", help="New content (set); reads stdin when omitted")
    agents_file.add_argument("--json", action="store_true", dest="json_output")

    recipes_cmd = sub.add_parser(
        "recipes", help="List, inspect, and scaffold recipes (agents + teams + workflows from one template)")
    recipes_sub = recipes_cmd.add_subparsers(dest="recipes_command", required=True)
    recipes_list = recipes_sub.add_parser("list", help="List available recipes (~/.jigga/recipes + bundled examples)")
    recipes_list.add_argument("--json", action="store_true", dest="json_output")
    recipes_show = recipes_sub.add_parser("show", help="Inspect a recipe: what it would scaffold")
    recipes_show.add_argument("recipe", help="Recipe name or path to a .md recipe")
    recipes_show.add_argument("--json", action="store_true", dest="json_output")
    recipes_delete = recipes_sub.add_parser(
        "delete", help="Delete a recipe's user-dir copy (reverts to bundled, if any); --uninstall also tears down what it installed")
    recipes_delete.add_argument("recipe", help="Recipe file name (stem)")
    recipes_delete.add_argument("--uninstall", action="store_true",
                                help="Also delete the installed team/agent (backed up, record-owned only)")
    recipes_delete.add_argument("--json", action="store_true", dest="json_output")
    recipes_cat = recipes_sub.add_parser("cat", help="Print a recipe's raw markdown")
    recipes_cat.add_argument("recipe")
    recipes_cat.add_argument(
        "--bundled", action="store_true",
        help="Read the bundled/default version, ignoring your user copy (for diffing against a new release)")
    recipes_save = recipes_sub.add_parser(
        "save", help="Write a recipe's markdown to your user recipes dir (~/.jigga/recipes — overrides bundled)")
    recipes_save.add_argument("recipe", help="Recipe file name (stem), e.g. marketing-team")
    recipes_save.add_argument("--content", help="Markdown content; reads stdin when omitted")
    recipes_save.add_argument("--json", action="store_true", dest="json_output")
    recipes_installed = recipes_sub.add_parser(
        "installed", help="Show installed recipes (provenance records) and any local edits/drift")
    recipes_installed.add_argument("--json", action="store_true", dest="json_output")
    recipes_scaffold = recipes_sub.add_parser(
        "scaffold", help="Scaffold a recipe: agent yamls + team + workflows + workspace")
    recipes_scaffold.add_argument("recipe", help="Recipe name (e.g. marketing-team) or path to a .md recipe")
    recipes_scaffold.add_argument("--id", dest="override_id",
                                  help="Override the scaffolded team/agent id (default: the recipe's id)")
    recipes_scaffold.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    recipes_scaffold.add_argument("--json", action="store_true", dest="json_output")
    recipes_create = recipes_sub.add_parser(
        "create", help="Create a NEW team/agent instance from a recipe: writes an editable user-dir copy "
                       "under --id (members become <id>-<role>) and scaffolds it — make as many copies as you like")
    recipes_create.add_argument("recipe", help="Source recipe name (e.g. marketing-team) or path")
    recipes_create.add_argument("--id", dest="new_id", required=True,
                                help="Id for the new instance (must be unused, e.g. marketing-east)")
    recipes_create.add_argument("--name", help="Display name for the new instance")
    recipes_create.add_argument("--overwrite", action="store_true",
                                help="Overwrite an existing user-dir recipe / files with this id")
    recipes_create.add_argument("--json", action="store_true", dest="json_output")
    recipes_lint = recipes_sub.add_parser(
        "lint", help="Check recipe(s) for structural / reference errors before scaffolding")
    recipes_lint.add_argument("recipe", nargs="?", help="Recipe name/path (default: lint all)")
    recipes_lint.add_argument("--json", action="store_true", dest="json_output")

    team = sub.add_parser("team", help="List/run teams, scaffold workspaces, and drive handoffs")
    team_sub = team.add_subparsers(dest="team_command", required=True)
    team_list = team_sub.add_parser("list", help="List teams (id, lead, members)")
    team_list.add_argument("--json", action="store_true", dest="json_output")
    team_get = team_sub.add_parser("get", help="Read a team's yaml (or one dotted key)")
    team_get.add_argument("team_id")
    team_get.add_argument("key", nargs="?")
    team_get.add_argument("--json", action="store_true", dest="json_output")
    team_set = team_sub.add_parser("set", help="Set a dotted key in a team's yaml (validated, audited)")
    team_set.add_argument("team_id")
    team_set.add_argument("key")
    team_set.add_argument("value")
    team_set.add_argument("--json", action="store_true", dest="json_output")
    for verb, help_text in (("disable", "Supervisor stops waking ALL of this team's members (reversible)"),
                            ("enable", "Re-enable a disabled team")):
        sp = team_sub.add_parser(verb, help=help_text)
        sp.add_argument("team_id")
        sp.add_argument("--json", action="store_true", dest="json_output")
    team_delete = team_sub.add_parser("delete", help="Delete a team: yaml, workspace, and its record-owned agents/workflows (backed up)")
    team_delete.add_argument("team_id")
    team_delete.add_argument("--json", action="store_true", dest="json_output")
    team_staff = team_sub.add_parser(
        "staff", help="Staff a membership-only member: write its agent definition into the recipe "
                      "(user-dir copy) and create-only scaffold the new agent")
    team_staff.add_argument("team_id")
    team_staff.add_argument("member_id")
    team_staff.add_argument("--role", dest="role_text", help="Role sentence for the new agent")
    team_staff.add_argument("--json", action="store_true", dest="json_output")
    team_add_agent = team_sub.add_parser(
        "add-agent", help="Add an existing agent to a team: copy its config into the team's recipe "
                          "as a new member and scaffold it")
    team_add_agent.add_argument("team_id")
    team_add_agent.add_argument("agent_id", help="Source agent to copy (see: jigga agents list)")
    team_add_agent.add_argument("--role", help="Member role key on the team (default: the source agent id)")
    team_add_agent.add_argument("--member-id", dest="member_id",
                                help="Id for the new member (default: <team>-<role>)")
    team_add_agent.add_argument("--overwrite", action="store_true", help="Replace an existing member with this id")
    team_add_agent.add_argument("--json", action="store_true", dest="json_output")
    team_files = team_sub.add_parser("files", help="List a team's workspace files (required/missing)")
    team_files.add_argument("team_id")
    team_files.add_argument("--json", action="store_true", dest="json_output")
    team_file = team_sub.add_parser("file", help="Read/write one team workspace file")
    team_file.add_argument("verb", choices=["get", "set"])
    team_file.add_argument("team_id")
    team_file.add_argument("name", help="Workspace-relative path, e.g. notes/plan.md")
    team_file.add_argument("--content", help="New content (set); reads stdin when omitted")
    team_file.add_argument("--json", action="store_true", dest="json_output")
    team_run = team_sub.add_parser("run")
    team_handoff = team_sub.add_parser("handoff", help="Fire a team's handoffs from a member (file-first)")
    team_handoff.add_argument("team_id")
    team_handoff.add_argument("--from", dest="from_member", required=True, help="Member completing/handing off")
    team_handoff.add_argument("--signal", help="Only fire handoffs whose `when` matches this signal")
    team_handoff.add_argument("--evidence", help="Path/reference to the work product being handed off")
    team_handoff.add_argument("--json", action="store_true", dest="json_output")
    team_decisions = team_sub.add_parser("decisions", help="Show a team's handoff decision log")
    team_decisions.add_argument("team_id")
    team_decisions.add_argument("--json", action="store_true", dest="json_output")
    team_lanes = team_sub.add_parser("lanes", help="Show a team's ticket-board lane vocabulary")
    team_lanes.add_argument("team_id")
    team_lanes.add_argument("--json", action="store_true", dest="json_output")
    team_init = team_sub.add_parser("init", help="Scaffold a team's shared workspace (notes/ + shared-context/ + roles/)")
    team_init.add_argument("team_id")
    team_init.add_argument("--json", action="store_true", dest="json_output")
    team_ws = team_sub.add_parser("workspace", help="Show a team's workspace path and files")
    team_ws.add_argument("team_id")
    team_run.add_argument("team_id")

    workspace = sub.add_parser("workspace", help="Manage shared workspaces (housekeeping)")
    workspace_sub = workspace.add_subparsers(dest="workspace_command", required=True)
    workspace_gc = workspace_sub.add_parser(
        "gc", help="Find (and with --apply, back up + remove) orphaned workspace dirs "
                   "— those with no owning team or solo agent")
    workspace_gc.add_argument("--apply", action="store_true",
                              help="Delete the orphans (backed up to state/backups/ first)")
    workspace_gc.add_argument("--protect", nargs="*", default=[],
                              help="Workspace ids to never touch")
    workspace_gc.add_argument("--json", action="store_true", dest="json_output")

    model = sub.add_parser("model", help="Inspect and test model execution")
    model_sub = model.add_subparsers(dest="model_command", required=True)
    model_test = model_sub.add_parser("test", help="Run a model call for a configured agent")
    model_test.add_argument("agent_id")
    model_test.add_argument("--prompt", required=True)
    model_test.add_argument("--dry-run", action="store_true", help="Do not call external providers")
    model_login = model_sub.add_parser(
        "login", help="Authenticate the ChatGPT-subscription provider (browser paste by default)"
    )
    model_login.add_argument("--device-code", action="store_true", dest="device_code",
                             help="Use the device-code flow (best for headless/remote)")
    model_sub.add_parser("status", help="Show the configured model provider and ChatGPT login state")
    model_use = model_sub.add_parser("use", help="Set the default model provider in config.yaml")
    model_use.add_argument("provider", choices=["chatgpt", "dry_run"], help="Provider to make default")
    model_use.add_argument("--model", help="Default model id for the provider")
    model_sub.add_parser("setup", help="Interactive onboarding: pick a model provider and authenticate")

    run = sub.add_parser("run", help="Run an agent manually")
    run.add_argument("kind", choices=["agent"])
    run.add_argument("agent_id")
    run.add_argument("--dry-run-model", action="store_true", help="Force model calls to use the dry-run provider")

    supervisor = sub.add_parser("supervisor", help="Supervisor daemon commands")
    supervisor_sub = supervisor.add_subparsers(dest="supervisor_command", required=True)
    supervisor_sub.add_parser("tick", help="Run one supervisor polling tick")
    supervisor_start = supervisor_sub.add_parser("start", help="Run the supervisor loop")
    supervisor_start.add_argument("--interval-seconds", type=float, default=60)
    supervisor_start.add_argument("--max-ticks", type=int, default=None, help="Stop after N ticks; useful for tests/demos")
    supervisor_start.add_argument("--channel-poll-seconds", type=int, default=DEFAULT_LONG_POLL_SECONDS,
                                  help="Channel long-poll timeout — how responsive chat is (0 = legacy per-tick poll)")

    service = sub.add_parser("service", help="Install the supervisor as an always-on service (launchd/systemd)")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    _system_help = "Target the system-level unit (survives logout; writing it needs root — run under sudo)"
    service_install = service_sub.add_parser("install", help="Register + start the supervisor as a service")
    service_install.add_argument("--interval-seconds", type=float, default=60)
    service_install.add_argument("--dry-run", action="store_true", help="Show the unit + commands without installing")
    service_install.add_argument("--system", action="store_true", help=_system_help)
    service_uninstall = service_sub.add_parser("uninstall", help="Stop and remove the supervisor service")
    service_uninstall.add_argument("--system", action="store_true", help=_system_help)
    service_status = service_sub.add_parser("status", help="Show whether the supervisor service is installed and running")
    service_status.add_argument("--system", action="store_true", help=_system_help)
    service_stop = service_sub.add_parser("stop", help="Stop the running supervisor (stays installed; restarts on login/boot)")
    service_stop.add_argument("--system", action="store_true", help=_system_help)
    service_start = service_sub.add_parser("start", help="Start an installed-but-stopped supervisor service")
    service_start.add_argument("--system", action="store_true", help=_system_help)

    task = sub.add_parser("task", help="Manage local task queue")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_create = task_sub.add_parser("create")
    task_create.add_argument("--title", required=True)
    task_create.add_argument("--description")
    task_create.add_argument("--assignee")
    task_create.add_argument("--workflow", dest="workflow_id")
    task_list = task_sub.add_parser("list")
    task_list.add_argument("--lane", help="Only tasks in this ticket lane")
    task_list.add_argument("--json", action="store_true", dest="json_output")
    task_set = task_sub.add_parser("set-state")
    task_set.add_argument("task_id")
    task_set.add_argument("state")
    task_move = task_sub.add_parser(
        "move", help="Move a ticket (team task) to another lane on its team's board")
    task_move.add_argument("task_id")
    task_move.add_argument("lane", help="Destination lane id (see: jigga team lanes <team>)")
    task_move.add_argument("--as", dest="as_member",
                           help="Act as this member/role (required to move OUT of a gated lane)")
    task_move.add_argument("--json", action="store_true", dest="json_output")
    return parser


def _cmd_init(args: argparse.Namespace) -> int:
    paths = init_runtime(args.home, examples=args.examples)
    print(f"Initialized JIGGA home: {paths.home}")
    if args.examples:
        print("Copied example agents and teams.")
    interactive = (not args.no_prompt) and sys.stdin.isatty() and sys.stdout.isatty()
    if interactive and resolve_default_agent(paths.agents) is None:
        if input("\nSet up your assistant now? [Y/n]: ").strip().lower() in {"", "y", "yes"}:
            run_onboarding(paths)
            # If channels were enabled before this agent existed (e.g. the old
            # default agent was deleted), close the reply loop for the new one.
            _regrant_enabled_channel_tools(paths)
        else:
            print("Skipped. Run `jigga setup` when you're ready.")
    maybe_prompt_after_init(paths, interactive=interactive)
    return 0


def _regrant_enabled_channel_tools(paths: Any) -> list[tuple[str, list[str]]]:
    """Re-apply channel tool grants after the default agent's yaml has been
    regenerated. `setup --overwrite` rebuilds the agent from bundled
    capabilities only, silently stripping the send tools + network egress that
    enabling a channel granted (bit RJ live: renaming the assistant broke
    Telegram replies until `jigga doctor` flagged it). Idempotent — a no-op
    when the grants are already present."""
    from jigga.runtime.audit import append_event

    config = read_yaml(paths.config)
    regranted: list[tuple[str, list[str]]] = []
    for channel, cfg in (config.get("channels") or {}).items():
        if not isinstance(cfg, dict) or not cfg.get("enabled"):
            continue
        granted = _grant_channel_tools(paths, channel, config)
        if granted:
            agent_id, items = granted
            append_event(paths.logs, "channel.tools_granted", channel=channel,
                         agent=agent_id, granted=items)
            regranted.append((agent_id, items))
    return regranted


def _cmd_setup(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    # input resolved at call time (not def-time default) so tests can patch it.
    run_onboarding(paths, input_fn=input, overwrite=args.overwrite)
    for agent_id, items in _regrant_enabled_channel_tools(paths):
        print(f"  Restored '{agent_id}' channel access — " + "; ".join(items))
    return 0


# Matches ANSI/VT escape sequences — including the focus-report events
# (ESC[I / ESC[O) a terminal injects into stdin when you alt-tab away during a
# blocking wait (e.g. a device-code login). Left in the buffer, those bytes get
# read as part of the *next* prompt's answer.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _sanitize_answer(raw: str) -> str:
    """Strip escape sequences + non-printable chars from a prompt answer, then
    normalize, so stray terminal control bytes can't corrupt a yes/no reply."""
    cleaned = _ANSI_RE.sub("", raw)
    cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    return cleaned.strip().lower()


def _drain_stdin() -> None:
    """Discard buffered terminal input before prompting, so focus-report escape
    sequences (or other type-ahead noise) accumulated during a prior blocking
    wait don't get consumed as the answer. POSIX TTY only; no-op elsewhere."""
    try:
        import termios
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (ImportError, OSError, ValueError):
        pass


def _confirm(question: str, *, default: bool, echo=print) -> bool:
    _drain_stdin()
    suffix = "[Y/n]" if default else "[y/N]"
    answer = _sanitize_answer(input(f"{question} {suffix}: "))
    if not answer:
        return default
    return answer in {"y", "yes"}


def _examples_setup(paths: Any, *, interactive: bool, echo: Callable[..., None] = print,
                    prompt: Callable[[str], str] | None = None) -> list[str]:
    """Offer the available example recipes (whatever is in the user + bundled
    recipe folders) and scaffold the selection. Non-interactive installs all —
    there's no one to ask. Returns the installed recipe ids."""
    if prompt is None:
        prompt = input  # resolved at call time so tests patching builtins.input work
    recipes = list_recipes(paths.home)
    if not recipes:
        echo("• No example recipes found.")
        return []
    if interactive and supports_picker():
        # Arrow-key checkbox picker (the OpenClaw onboarding style).
        echo("")
        options = [Option(label=f"{r['id']:24} {r['kind']:6}", detail=r.get("description") or "")
                   for r in recipes]
        _drain_stdin()
        picked = multi_select("Install example recipes", options)
        if not picked:
            echo("• Skipped examples. Install any later with `jigga recipes scaffold <name>`.")
            return []
        chosen = [recipes[i] for i in picked]
    elif interactive:
        # Typed fallback for pipes/dumb terminals: numbers/names, 'all', Enter.
        echo("\nExample recipes available:")
        for index, entry in enumerate(recipes, 1):
            echo(f"  {index}) {entry['id']:24} {entry['kind']:6} {entry.get('description') or ''}")
        _drain_stdin()
        raw = _sanitize_answer(prompt("Install which? [numbers/names, 'all', Enter for none]: "))
        if raw in {"", "none", "n"}:
            echo("• Skipped examples. Install any later with `jigga recipes scaffold <name>`.")
            return []
        if raw in {"all", "a", "*"}:
            chosen = list(recipes)
        else:
            chosen = []
            for token in [t for t in re.split(r"[,\s]+", raw) if t]:
                if token.isdigit() and 1 <= int(token) <= len(recipes):
                    chosen.append(recipes[int(token) - 1])
                    continue
                match = next((r for r in recipes
                              if r["id"] == token or Path(r["source"]).stem == token), None)
                if match is not None:
                    chosen.append(match)
                else:
                    echo(f"  ! Unknown selection {token!r} (skipped)")
    else:
        chosen = list(recipes)

    installed: list[str] = []
    for entry in chosen:
        if entry["id"] in installed:
            continue
        recipe = load_recipe(Path(entry["source"]))
        if recipe.kind == "agent":
            scaffold_agent(paths.home, recipe, agents_dir=paths.agents,
                           workflows_dir=paths.workflows)
        else:
            scaffold_team(paths.home, recipe, agents_dir=paths.agents, teams_dir=paths.teams,
                          workflows_dir=paths.workflows)
        installed.append(entry["id"])
    if installed:
        echo(f"✓ Installed: {', '.join(installed)}")
    return installed


def _cmd_onboard(args: argparse.Namespace) -> int:
    """Guided end-to-end setup, OpenClaw-`onboard`-style: one command takes a
    fresh clone from an empty runtime to a configured (optionally always-on)
    assistant by chaining the steps that already exist as standalone commands.

    Non-interactive scaffolds sane defaults and skips the inherently
    interactive model/channel wizards; --install-daemon still runs since it's
    flag-driven."""
    interactive = not args.non_interactive
    paths = init_runtime(args.home)
    print(f"\n=== JIGGA onboarding === (home: {paths.home})")

    # 0. Example recipes — list whatever's in the recipe folders and install
    #    the user's selection (everything, when there's no one to ask).
    if args.examples:
        _examples_setup(paths, interactive=interactive)

    # 1. Who the assistant works for + the default agent (USER.md + agent yaml).
    run_onboarding(
        paths,
        input_fn=input if interactive else (lambda _prompt: ""),
        overwrite=args.overwrite,
    )
    # A re-run with --overwrite regenerates the agent yaml — restore the tool
    # grants any already-enabled channel had given it (idempotent).
    for agent_id, items in _regrant_enabled_channel_tools(paths):
        print(f"  Restored '{agent_id}' channel access — " + "; ".join(items))

    # 2. Model — needed for agents to think. Interactive only (provider choice
    #    + secrets); offered unless skipped.
    if interactive and not args.skip_model and _confirm(
        "\nConnect a model now so agents can think?", default=True
    ):
        _model_setup(paths)
    elif not interactive and not args.skip_model:
        print("• Skipped model setup (non-interactive). Run `jigga model setup` later.")

    # 3. Channel — how the person actually talks to their assistant. Asked as a
    #    choice of driver rather than a yes/no, because "connect a channel?"
    #    doesn't tell anyone what their options are or what each one costs.
    if interactive and not args.skip_channels:
        print("\nHow do you want to talk to me?")
        for i, (name, (_, blurb)) in enumerate(sorted(_CHANNEL_CATALOG.items()), 1):
            print(f"  {i}. {name} — {blurb}")
        print(f"  {len(_CHANNEL_CATALOG) + 1}. Just this terminal for now")
        picked = input("Choose [Enter for terminal]: ").strip()
        names = sorted(_CHANNEL_CATALOG)
        if picked.isdigit() and 1 <= int(picked) <= len(names):
            _channels_setup(paths, preselected=names[int(picked) - 1])
        elif picked in _CHANNEL_CATALOG:
            _channels_setup(paths, preselected=picked)
        else:
            print("• Terminal only. Run `jigga channels setup` whenever you want another way in.")
    elif not interactive and not args.skip_channels:
        print("• Skipped channel setup (non-interactive). Run `jigga channels setup` later.")

    # 3.5 Web search — optional provider so `web.search` works from any host
    # (the zero-config DDG-lite endpoint bot-blocks datacenter IPs; #158).
    if interactive and _confirm(
        "\nSet up a web-search provider for agents (SearXNG or Brave API)?", default=False
    ):
        pick = input("  1. searxng — metasearch instance, no API key\n"
                     "  2. brave-search — Brave Search API key (free tier)\n"
                     "Choose [1]: ").strip()
        install_capability(paths, name="brave-search" if pick == "2" else "searxng")

    # 4. Start it now + keep it running. Registering the user service is the
    #    right "start now": it runs in the background, independent of this shell
    #    (we can't activate the parent shell's venv from here), and survives
    #    logout/reboot. Forced by --install-daemon; otherwise offered.
    install_daemon = args.install_daemon
    if not install_daemon and interactive:
        install_daemon = _confirm(
            "\nStart the assistant now and keep it running across reboots?", default=True)
    started_service = False
    if install_daemon:
        from jigga.runtime.service import install_service
        result = install_service(paths, interval_seconds=args.service_interval)
        backend = result["backend"]
        if backend == "unsupported":
            print(f"\n! Couldn't install a background service here: {result.get('instructions', '')}")
        elif result.get("started"):
            started_service = True
            print(f"\n✓ Supervisor running as a {backend} service (survives logout/reboot).")
        else:
            print(f"\n! Wrote the {backend} unit at {result.get('unit_path')}, "
                  "but starting it reported a problem — see `jigga service status`.")

    # 5. Next steps.
    print("\n✓ Onboarding complete.")
    if started_service:
        print("  It's running in the background now — `jigga service status` to check it.")
    else:
        print("  Start it:  `jigga supervisor start` (foreground) "
              "or `jigga service install` (always-on background).")
    print("  To run `jigga` commands in this shell, activate the venv first "
          "(e.g. `source .venv/bin/activate`).")
    return 0


def _cmd_state(args: argparse.Namespace) -> int:
    result = inspect_state(args.home)
    if args.json_output:
        print_json(result)
    else:
        print(f"JIGGA home: {result['home']}")
        print(f"Agents: {', '.join(result['agents']) if result['agents'] else 'none'}")
        print(f"Teams: {', '.join(result['teams']) if result['teams'] else 'none'}")
        print(f"Workflows: {', '.join(result['workflows']) if result['workflows'] else 'none'}")
        print(f"Memory scopes: {', '.join(result['memory_scopes']) if result['memory_scopes'] else 'none'}")
        print(f"Tasks: {len(result['tasks'])}")
    return 0


_DOCTOR_GLYPH = {"ok": "✓", "warn": "⚠", "fail": "✗"}



def _edited_footer(edited: list[dict]) -> None:
    """The blunt-instrument escape hatch for kept files: re-scaffolding a
    recipe with --overwrite replaces ALL of its files (the per-file path is
    the picker)."""
    recipe = next((e.get("recipe") for e in edited if e.get("recipe")), None)
    if recipe:
        print("  Replace a recipe's files wholesale later (loses your edits):")
        print(f"    jigga recipes scaffold {recipe} --overwrite")


def _select_recipe_changes(plan: dict) -> tuple[list[dict], list[str]]:
    """ONE picker over every recipe-file change, during plan review (before the
    apply confirm): pristine updates arrive PRE-selected (safe — deselect to
    keep your copy as-is); edited-divergent files arrive UNselected (opting in
    replaces them, with a backup first). Returns (chosen artifact actions,
    chosen edited paths)."""
    artifact_actions = [a for a in plan.get("actions", []) if str(a.get("kind", "")).startswith("artifact.")]
    edited = plan.get("edited") or []
    if not artifact_actions and not edited:
        return [], []
    if not supports_picker():
        # No raw-mode picker here (dumb term): pristine updates stay in the
        # plan under the apply confirm; edited files are kept, footer printed.
        if edited:
            _edited_footer(edited)
        return artifact_actions, []
    options = ([Option(label=a["detail"]["path"], detail=a["description"], selected=True)
                for a in artifact_actions]
               + [Option(label=e["path"],
                         detail=f"local edits — backed up if replaced (recipe: {e.get('recipe')})",
                         selected=False)
                  for e in edited])
    print()
    picked = multi_select("Recipe file changes — select what to apply", options) or []
    chosen_actions = [artifact_actions[i] for i in picked if i < len(artifact_actions)]
    chosen_paths = [edited[i - len(artifact_actions)]["path"]
                    for i in picked if i >= len(artifact_actions)]
    kept = [e for e in edited if e["path"] not in set(chosen_paths)]
    if kept:
        _edited_footer(kept)
    return chosen_actions, chosen_paths




def _cmd_plugins(args: argparse.Namespace) -> int:
    from jigga.runtime.plugins import (
        install_plugin,
        list_plugins,
        load_app_manifest,
        plugin_dir,
        uninstall_plugin,
        _service_env,
    )
    from jigga.runtime.service import install_app_service, status_app_service, uninstall_app_service

    paths = get_paths(args.home)
    if args.plugins_command == "install":
        try:
            summary = install_plugin(paths, args.source, service=not args.no_service)
        except ValueError as exc:
            print(f"! {exc}")
            return 1
        if args.json_output:
            print_json(summary)
        else:
            print(f"✓ Installed plugin {summary['name']!r} v{summary['version']} → {summary['dir']}")
            findings = (summary.get("scan") or {}).get("findings") or []
            for finding in findings:
                print(f"  ⚠ scan: {finding.get('message', finding)}")
            service = summary.get("service")
            if service and service.get("started"):
                where = f"http://localhost:{summary['port']}" if summary.get("port") else "(no port declared)"
                print(f"  Running as a {service['backend']} service — {where}")
            elif service is not None:
                print("  Service registered but not confirmed running — check `jigga plugins status "
                      + summary["name"] + "`")
        return 0
    if args.plugins_command == "list":
        plugins = list_plugins(paths)
        if args.json_output:
            print_json(plugins)
        elif not plugins:
            print("No plugins installed. Try: jigga plugins install https://github.com/JIGGAI/jiggaview")
        else:
            for plugin in plugins:
                state = "running" if plugin["running"] else ("stopped" if plugin["installed_service"] else "no service")
                port = f"  :{plugin['port']}" if plugin.get("port") else ""
                print(f"{plugin['name']:20} v{plugin['version']:8} {state:10}{port}  {plugin['summary']}")
        return 0
    if args.plugins_command == "status":
        result = status_app_service(args.name)
        print_json(result) if args.json_output else print(
            f"{args.name}: " + ("running" if result.get("running") else
                                "installed, not running" if result.get("installed") else "not installed"))
        return 0
    if args.plugins_command == "start":
        target = plugin_dir(paths.home, args.name)
        manifest = target / "manifest.yaml"
        if not manifest.exists():
            print(f"! Plugin {args.name!r} is not installed")
            return 1
        capability = load_app_manifest(manifest)
        result = install_app_service(capability.name, capability.run, cwd=target,
                                     env=_service_env(paths, capability), logs_dir=paths.logs)
        print_json(result) if args.json_output else print(
            f"{args.name}: " + ("started" if result.get("started") else "start reported a problem — see --json"))
        return 0 if result.get("started") or result.get("backend") != "unsupported" else 1
    if args.plugins_command == "stop":
        result = uninstall_app_service(args.name)
        print_json(result) if args.json_output else print(f"{args.name}: stopped")
        return 0
    if args.plugins_command == "uninstall":
        try:
            result = uninstall_plugin(paths, args.name)
        except ValueError as exc:
            print(f"! {exc}")
            return 1
        print_json(result) if args.json_output else print(f"✓ Uninstalled {args.name!r}")
        return 0
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    import json as _json

    from jigga.runtime.audit import append_event
    from jigga.runtime.config_edit import coerce_value, get_path, set_path, unset_path

    paths = get_paths(args.home)
    config = read_yaml(paths.config) if paths.config.exists() else {}

    if args.config_command == "get":
        value = get_path(config, args.key) if args.key else config
        if args.json_output:
            print_json(value)
        elif isinstance(value, (dict, list)):
            print(_json.dumps(value, indent=2, default=str))
        else:
            print(value)
        return 0
    if args.config_command == "set":
        value = coerce_value(args.value)
        try:
            old = set_path(config, args.key, value)
        except ValueError as exc:
            print(f"! {exc}")
            return 1
        write_yaml(paths.config, config)
        append_event(paths.logs, "config.changed", key=args.key, old=old, new=value)
        if args.json_output:
            print_json({"key": args.key, "old": old, "new": value})
        else:
            print(f"{args.key}: {old!r} → {value!r}")
        return 0
    if args.config_command == "unset":
        old = unset_path(config, args.key)
        write_yaml(paths.config, config)
        append_event(paths.logs, "config.changed", key=args.key, old=old, new=None)
        if args.json_output:
            print_json({"key": args.key, "old": old, "new": None})
        else:
            print(f"{args.key}: {old!r} → (unset)" if old is not None else f"{args.key}: (was not set)")
        return 0
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    """Plan → review → confirm. Interactive runs end with an apply prompt so
    no --apply re-run is needed (--apply skips it for automation; --dry-run
    never prompts or applies), then a per-item picker for files with local
    edits + shipped updates (never auto-replaced; backed up when chosen)."""
    import sys as _sys

    from jigga.runtime.update import apply_update, plan_update

    paths = get_paths(args.home)
    plan = plan_update(paths)
    actions, notices, edited = plan["actions"], plan["notices"], plan.get("edited") or []

    artifact_actions = [a for a in actions if str(a.get("kind", "")).startswith("artifact.")]
    other_actions = [a for a in actions if not str(a.get("kind", "")).startswith("artifact.")]
    interactive = _sys.stdin.isatty()
    # Recipe-file changes route through the picker on interactive runs — they
    # appear there, not in the flat plan list (RJ: any recipe change is a
    # selection option, then the apply confirm).
    picker_mode = interactive and not args.apply and not args.dry_run

    if args.json_output and args.dry_run:
        print_json(plan)
        return 0
    if not args.json_output:
        if not actions and not notices:
            print("✓ Everything up to date — nothing to reconcile.")
            return 0
        listed = other_actions if picker_mode else actions
        if listed:
            print("Planned changes:")
            for action in listed:
                print(f"  • {action['description']}")
        for notice in notices:
            print(f"  ~ {notice}")
    if args.dry_run:
        if edited:
            _edited_footer(edited)
        print("\n(dry run — apply with: jigga update --apply)")
        return 0

    # Non-interactive without --apply: show everything, mutate nothing.
    if not args.apply and not interactive:
        if args.json_output:
            print_json({**plan, "applied": False})
        else:
            print("\nNot applied (non-interactive). Apply with: jigga update --apply")
            _edited_footer(edited)
        return 0

    # Review phase, BEFORE any confirm: every recipe-file change is a picker
    # option (pristine pre-selected, edited opt-in). The single apply confirm
    # below then covers the selections + the remaining plan.
    chosen_actions: list[dict] = list(artifact_actions)
    selected: list[str] = []
    if picker_mode:
        chosen_actions, selected = _select_recipe_changes(plan)
        for action in chosen_actions:
            print(f"  • {action['description']}")
        for rel in selected:
            print(f"  • replace {rel} (your edits are backed up first)")

    if not other_actions and not chosen_actions and not selected:
        print("\nNothing to apply.")
        return 0

    do_apply = args.apply or _confirm("\nApply these changes?", default=True)
    if not do_apply:
        print("Not applied.")
        return 0

    # Mutate: replacements first, then the planned actions — the planner puts
    # service refresh/restart last, so the daemon comes back on final state.
    from jigga.runtime.update import overwrite_edited

    apply_plan = {**plan, "actions": other_actions + chosen_actions}
    overwrite_results = overwrite_edited(paths, plan, selected) if selected else None
    results = apply_update(paths, apply_plan) if apply_plan["actions"] else {"applied": [], "errors": []}

    if args.json_output:
        print_json({**plan, "applied": True, "results": results, "overwrites": overwrite_results})
    else:
        if overwrite_results:
            for rel in overwrite_results["replaced"]:
                print(f"  ✓ replaced {rel}")
            for backup in overwrite_results["backups"]:
                print(f"    backup: {backup}")
            for error in overwrite_results["errors"]:
                print(f"  ! {error}")
        for item in results["applied"]:
            print(f"  ✓ {item}")
        for error in results["errors"]:
            print(f"  ! {error}")
        total = len(results["applied"]) + len((overwrite_results or {}).get("replaced", []))
        all_errors = results["errors"] + list((overwrite_results or {}).get("errors", []))
        print(f"\n✓ Applied {total} change(s)."
              + (f"  {len(all_errors)} error(s)." if all_errors else ""))
    all_errors = results["errors"] + list((overwrite_results or {}).get("errors", []))
    return 1 if all_errors else 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from jigga.runtime.doctor import run_checks, run_fixes

    paths = get_paths(args.home)
    probe = getattr(args, "probe", True)
    report = run_checks(paths, probe=probe)
    fixes: list[dict] = []
    if getattr(args, "fix", False):
        fixes = run_fixes(paths, report)
        report = run_checks(paths, probe=probe)  # re-check so the report reflects what was fixed
    if args.json_output:
        out = report.to_dict()
        if getattr(args, "fix", False):
            out["fixes"] = fixes
        print_json(out)
    else:
        if fixes:
            print("Applied fixes:")
            for f in fixes:
                print(f"  {'✓' if f['fixed'] else '✗'} {f['check']}: {f['message']}")
            print()
        for c in report.checks:
            print(f"{_DOCTOR_GLYPH.get(c.status, '?')} {c.name}: {c.detail}")
            if c.hint and c.status != "ok":
                print(f"    → {c.hint}")
        s = report.to_dict()["summary"]
        print(f"\n{s['ok']} ok, {s['warn']} warning(s), {s['fail']} problem(s).")
        if report.failed:
            print("Doctor found problems that need attention.")
        elif report.warned:
            print("No blocking problems; warnings are optional to address.")
        else:
            print("All good. 🦞")
    return 1 if report.failed else 0


def _cmd_memory(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.memory_command == "inspect":
        print_json(inspect_memory(paths.memory))
    elif args.memory_command == "search":
        results = search_memory(paths.memory, args.query, scope=args.scope, team=args.team,
                                limit=args.limit, rebuild=args.rebuild)
        if args.json_output:
            print_json(results)
        elif not results:
            print("No matches.")
        else:
            for r in results:
                print(f"[{r['layer']}] {r['path']}")
                print(f"    {r['snippet']}")
    elif args.memory_command == "reindex":
        count = rebuild_index(paths.memory)
        print(f"Indexed {count} memory document(s).")
    elif args.memory_command == "compact":
        summary = compact_memory(paths.home, dry_run=args.dry_run)
        if args.json_output:
            print_json(summary)
        else:
            verb = "Would archive" if summary["dry_run"] else "Archived"
            print(f"{verb}: {len(summary['raw_archived'])} raw entr(ies), "
                  f"{summary['facts_archived']} stale fact(s), "
                  f"{len(summary['tasks_archived'])} finished task(s).")
    elif args.memory_command == "proposals":
        pending = list_proposals(paths.memory.parent, args.team)
        if args.json_output:
            print_json(pending)
        elif not pending:
            print("No pending memory proposals.")
        else:
            for p in pending:
                print(f"{p['id']}  [{p['team']}/{p.get('type')}]  {p['text']}")
    elif args.memory_command in ("approve", "reject"):
        resolved = apply_proposal(paths.memory.parent, args.proposal_id,
                                  approve=args.memory_command == "approve", team_id=args.team)
        if resolved is None:
            print(f"No pending proposal with id {args.proposal_id!r}.")
            return 1
        print(f"{resolved['status'].title()} {args.proposal_id}"
              + (f" → committed as {resolved['memory_id']}" if resolved.get("memory_id") else ""))
    return 0


def _cmd_workflow(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.workflow_command == "plan":
        workflows = load_workflows(paths.workflows)
        workflow = workflows.get(args.workflow_id)
        if workflow is None:
            raise ValueError(f"Workflow not found: {args.workflow_id}")
        plan = plan_workflow(
            workflow,
            load_agents(paths.agents),
            default_mode=default_permission_mode(paths.home),
            registry=CapabilityRegistry.load(
                user_capabilities=paths.capabilities,
                project_capabilities=project_capabilities_dir(
                    resolve_project_root(args.project)
                ),
                approvals_dir=paths.policies,
            ),
        )
        if args.json_output:
            print_json(plan)
        else:
            print(f"Workflow: {workflow.id} — {workflow.name}")
            print(f"Status: {workflow.status}")
            print(f"Permissions: {', '.join(plan['permissions']) if plan['permissions'] else 'none declared'}")
            for item in plan.get("steps") or plan.get("nodes") or []:
                reason = f": {item['policy']['reason']}" if item["policy"].get("reason") else ""
                label = item.get("action") or item.get("type")
                print(f"- {item['id']}: {label} [{item['policy']['status']}{reason}]")
            for problem in plan.get("problems") or []:
                print(f"! {problem}")
            print("Plan: runnable" if plan["can_run"] else "Plan: blocked / approval needed")
    elif args.workflow_command == "run":
        print_json(
            run_workflow(
                paths,
                args.workflow_id,
                project_capabilities=project_capabilities_dir(
                    resolve_project_root(args.project)
                ),
            )
        )
    elif args.workflow_command == "suggest":
        from jigga.core.config import resolve_default_agent

        print_json(suggest_workflows(paths.logs, min_count=args.min_count,
                                     orchestrator=resolve_default_agent(paths.agents)))
    elif args.workflow_command == "suggestions":
        from jigga.runtime.discovery import open_suggestions

        print_json(open_suggestions(paths.home, paths.logs, min_count=args.min_count))
    elif args.workflow_command == "list":
        workflows = [{"id": wf.id, "name": wf.name, "status": getattr(wf, "status", None)}
                     for wf in load_workflows(paths.workflows).values()]
        print_json(workflows)
    elif args.workflow_command == "apply":
        from jigga.core.config import resolve_default_agent

        print_json(apply_suggestion(paths.workflows, args.suggestion_id, paths.logs,
                                    approve=args.approve, orchestrator=resolve_default_agent(paths.agents)))
    elif args.workflow_command == "runs":
        from jigga.runtime.workflow_engine import list_runs

        records = list_runs(paths, args.workflow_id, active_only=args.active)
        if args.json_output:
            print_json(records)
        else:
            from collections import Counter

            for record in records:
                nodes = record.get("nodes", {})
                counts = Counter(v["status"] for v in nodes.values())
                summary = ", ".join(f"{status}={n}" for status, n in sorted(counts.items()))
                print(f"- {record['id']}  {record['workflow_id']}  [{record['status']}]  {summary}")
                # An ask that was never delivered reads exactly like one nobody
                # has answered yet unless it's called out here.
                for node_id, state in sorted(nodes.items()):
                    if state.get("status") == "awaiting_approval" and state.get("delivery") == "undelivered":
                        print(f"    ! {node_id}: approval NOT DELIVERED ({state.get('delivery_error')}) "
                              f"— approve locally with `jigga approve {state.get('approval_code')}`")
            if not records:
                print("No v2 workflow runs.")
    elif args.workflow_command == "resume":
        from jigga.runtime.workflow_engine import advance_run, load_run

        record = load_run(paths, args.run_id)
        if record is None:
            raise ValueError(f"Run not found: {args.run_id}")
        print_json(advance_run(paths, record))
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    result = plan_runtime(get_paths(args.home))
    if args.json_output:
        print_json(result)
    else:
        print(f"Plan: {result['status']}")
        for change in result["changes"]:
            approval = f" requires {change['requires_approval']}" if change.get("requires_approval") else ""
            print(f"- {change['change']} {change['path']}{approval}")
    return 0


def _cmd_apply(args: argparse.Namespace) -> int:
    print_json(apply_runtime(get_paths(args.home), approve=args.approve))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    result = validate_runtime_configs(get_paths(args.home))
    problems = result.get("problems", [])
    if args.json_output:
        print_json(result)
    else:
        for kind, values in result.items():
            if kind == "problems":
                continue
            print(f"{kind}: {', '.join(values) if values else 'none'}")
        if problems:
            print("problems:")
            for p in problems:
                print(f"  - {p}")
        else:
            print("problems: none")
    # Non-zero exit when there are error-level problems (warnings don't fail).
    return 1 if any(not p.startswith("warning:") for p in problems) else 0


def _cmd_capabilities(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    registry = CapabilityRegistry.load(
        user_capabilities=paths.capabilities,
        project_capabilities=project_capabilities_dir(
            resolve_project_root(args.project)
        ),
        approvals_dir=paths.policies,
    )
    if args.capabilities_command == "list":
        print_json(registry.to_index())
    elif args.capabilities_command == "pending":
        print_json([cap.to_dict() for cap in registry.list_pending()])
    elif args.capabilities_command == "inspect":
        capability = registry.get(args.name)
        if capability is None:
            raise ValueError(f"Capability not found: {args.name}")
        print_json(capability.to_dict())
    elif args.capabilities_command == "validate":
        capability = load_capability_manifest(args.path)
        report = scan_capability(capability, pack_dir=args.path.parent)
        print_json(
            {
                "status": "valid",
                "capability": capability.to_dict(),
                "scan": report.to_dict(),
            }
        )
    elif args.capabilities_command == "approve":
        capability = load_capability_manifest(args.path)
        report = scan_capability(capability, pack_dir=args.path.parent)
        if not args.confirm:
            print_json(
                {
                    "status": "needs_approval",
                    "capability": capability.to_dict(),
                    "scan": report.to_dict(),
                    "hint": "Re-run with --approve to record the approval. Review the scan findings first.",
                }
            )
            return 0
        entry = record_approval(paths.policies, capability)
        print_json(
            {
                "status": "approved",
                "capability": capability.name,
                "approval": entry,
                "scan": report.to_dict(),
            }
        )
    elif args.capabilities_command == "install":
        return install_capability(paths, name=args.name)
    elif args.capabilities_command == "uninstall":
        return uninstall_capability(paths, name=args.name)
    elif args.capabilities_command == "list-available":
        return list_available_capabilities()
    return 0


def _cmd_auth(args: argparse.Namespace) -> int:
    if args.auth_command == "status":
        print_json([status.to_dict() for status in auth_status()])
    elif args.auth_command == "login":
        exit_code = run_external_login(args.backend)
        return exit_code
    return 0


def _cmd_calendar(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.calendar_command == "status":
        client = load_client_config(paths.secrets)
        tokens = load_tokens(paths.secrets)
        payload = {
            "client_config_present": client is not None,
            "client_config_path": str(client_config_path(paths.secrets)),
            "tokens_present": tokens is not None,
            "tokens_path": str(tokens_path(paths.secrets)),
            "token_expired": tokens.is_expired() if tokens else None,
            "scope": tokens.scope if tokens else None,
            "expires_at": tokens.expires_at if tokens else None,
        }
        print_json(payload)
        return 0
    if args.calendar_command == "login":
        if load_client_config(paths.secrets) is None:
            print(
                "No Google OAuth client config found. Run "
                "`jigga capabilities install google-calendar` first.",
                file=sys.stderr,
            )
            return 1
        run_oauth_flow(paths.secrets)
        print("Login successful. Tokens stored.")
        return 0
    if args.calendar_command == "logout":
        removed = delete_tokens(paths.secrets)
        print("Tokens removed." if removed else "No tokens to remove.")
        return 0
    return 0


def _cmd_gog(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.gog_command == "status":
        print_json(gog_auth_status(paths.secrets))
        return 0
    if args.gog_command == "login":
        services = args.services or ",".join(DEFAULT_SERVICES)
        exit_code = run_gog_interactive(
            paths.secrets, ["auth", "add", args.email, "--services", services]
        )
        if exit_code == 0:
            print("gog login complete.")
        else:
            print(f"gog login failed (exit {exit_code}).", file=sys.stderr)
        return exit_code
    if args.gog_command == "logout":
        pw_path = keyring_password_path(paths.secrets)
        if pw_path.exists():
            pw_path.unlink()
            print(
                "Removed JIGGA's stored gog keyring password. "
                "This does NOT revoke Google access — run `gog auth remove` "
                "or revoke in your Google account to fully disconnect."
            )
        else:
            print("No stored gog keyring password to remove.")
        return 0
    return 0


def _cmd_telegram(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.telegram_command == "status":
        token = telegram_load_bot_token(paths.secrets)
        print_json(
            {
                "token_present": token is not None,
                "token_path": str(telegram_bot_token_path(paths.secrets)),
                "allowed_chat_ids": sorted(telegram_allowed_chat_ids(paths.home)),
                "offset": telegram_load_offset(paths.home),
            }
        )
        return 0
    if args.telegram_command == "discover":
        result = telegram_poll_messages(paths.home, discover=True)
        print_json(result)
        return 0
    if args.telegram_command == "logout":
        token_path = telegram_bot_token_path(paths.secrets)
        if token_path.exists():
            token_path.unlink()
            print("Removed stored Telegram bot token.")
        else:
            print("No stored Telegram bot token to remove.")
        return 0
    return 0


def _cmd_channels(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.channels_command == "status":
        print_json(
            [{"channel": name, "config": cfg} for name, cfg in enabled_channels(paths.home)]
        )
        return 0
    if args.channels_command == "setup":
        _channels_setup(paths)
        return 0
    if args.channels_command == "listen":
        result = channel_listen(
            paths.home,
            paths.logs,
            paths.tasks,
            paths.agents,
            long_poll_seconds=args.long_poll_seconds,
            max_cycles=args.max_cycles,
            process_agents=not args.no_process,
        )
        print_json(
            {
                "status": result["status"],
                "cycles": result["cycles"],
                "stopped_by_signal": result["stopped_by_signal"],
            }
        )
        return 0
    return 0


def _ensure_webchat_enabled(paths: Any, *, target_agent: str | None = None) -> dict[str, Any]:
    """First use of webchat enables it — typing in the local chat IS the opt-in
    (no token, no auth; only someone who can reach the jiggaview host gets here).
    Sets `channels.webchat.enabled`, claims the default-channel slot if nothing
    else has, and grants the reply tool (same close-the-loop grant `channels
    setup` does) — to the routed default agent and, when the send addresses a
    specific agent (the chat page's picker), to that agent too. Idempotent."""
    from jigga.runtime.audit import append_event

    config = read_yaml(paths.config)
    entry = config.setdefault("channels", {}).setdefault("webchat", {})
    enabled_now = not entry.get("enabled")
    if enabled_now:
        entry["enabled"] = True
        entry.setdefault("activation", "always")
        ensure_default_channel(config, "webchat")
        write_yaml(paths.config, config)
        append_event(paths.logs, "channel.webchat.enabled", activation=entry["activation"])
    grants = [_grant_channel_tools(paths, "webchat", config)]
    if target_agent:
        grants.append(_grant_channel_tools(paths, "webchat", config, agent_id=target_agent))
    for granted in grants:
        if granted:
            agent_id, items = granted
            append_event(paths.logs, "channel.tools_granted", channel="webchat",
                         agent=agent_id, granted=items)
    return {"enabled_now": enabled_now, "granted": next((g for g in grants if g), None)}


def _cmd_webchat(args: argparse.Namespace) -> int:
    from jigga.runtime import webchat
    from jigga.runtime.channel_listener import ingest_once

    paths = get_paths(args.home)
    target_agent = getattr(args, "agent", None)
    # Each agent gets its own thread by default; the plain default-agent chat
    # stays on the classic `web` conversation.
    conversation = (getattr(args, "conversation", None) or target_agent
                    or webchat.DEFAULT_CONVERSATION)
    if args.webchat_command == "send":
        if target_agent and not (paths.agents / f"{target_agent}.yaml").exists():
            print(f"No agent named {target_agent!r} (see `jigga agents list`).", file=sys.stderr)
            return 1
        setup = _ensure_webchat_enabled(paths, target_agent=target_agent)
        # Snapshot before the send so --wait can return exactly the replies this
        # message produced (ids, not counts — robust to interleaved writers).
        seen = {e.get("id") for e in webchat.history(paths.home, conversation_id=conversation, limit=1000)}
        entry = webchat.append_inbound(paths.home, args.text,
                                       conversation_id=conversation, sender=args.sender,
                                       target_agent=target_agent)
        replies: list[dict[str, Any]] = []
        if args.wait:
            # Webchat-only ingest: synchronous send must never block behind
            # another channel's long-poll. The offset advances here, so the
            # supervisor's backstop poll won't double-process this message.
            ingest_once(paths.home, paths.logs, paths.tasks, paths.agents,
                        long_poll_seconds=0, process_agents=True, only_channel="webchat")
            replies = [e for e in webchat.history(paths.home, conversation_id=conversation, limit=1000)
                       if e.get("sender") == "agent" and e.get("id") not in seen]
        result = {
            "status": "ok",
            "message": entry,
            "replies": replies,
            "channel_enabled_now": setup["enabled_now"],
        }
        if args.json_output:
            print_json(result)
        else:
            print(f"sent {entry['id']} → {conversation}")
            for reply in replies:
                print(f"agent: {reply.get('text')}")
            if args.wait and not replies:
                print("(no reply yet — the agent run produced none; check `jigga logs tail`)")
        return 0
    if args.webchat_command == "history":
        entries = webchat.history(paths.home, conversation_id=conversation, limit=args.limit)
        if args.json_output:
            print_json(entries)
        else:
            for e in entries:
                print(f"[{e.get('ts')}] {e.get('sender')}: {e.get('text')}")
        return 0
    if args.webchat_command == "conversations":
        convs = webchat.list_conversations(paths.home)
        if args.json_output:
            print_json(convs)
        else:
            for c in convs:
                print(f"{c['conversation_id']:24} {(c.get('agent') or 'default'):20} "
                      f"{c['count']:4}  {c.get('last_ts', '')[:16]}  {c.get('last_text', '')[:50]}")
        return 0
    return 0


def _cmd_approvals(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.approvals_command == "list":
        pend = pending_approvals(paths.approvals)
        if args.json_output:
            print_json(pend)
        elif not pend:
            print("No pending approvals.")
        else:
            for a in pend:
                print(f"{a['code']}  {a['action']:24} agent={a['agent_id']} task={a['task_id']} "
                      f"reason={a.get('reason') or ''}")
        return 0
    approved = args.approvals_command == "approve"
    record = resolve_and_requeue(paths.approvals, paths.tasks, args.code, approved=approved)
    if record is None:
        print(f"No pending approval with code {args.code!r}.")
        return 1
    print(f"{'Approved' if approved else 'Denied'} {args.code}."
          + (" Task re-queued — the agent will retry the action." if approved else ""))
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.logs_command == "tail":
        events = tail_events(paths.logs, args.count)
        if args.json_output:
            print_json(events)
        else:
            for event in events:
                print(format_event(event))
    elif args.logs_command == "rotate":
        result = rotate_logs(paths.home, paths.logs)
        if args.json_output:
            print_json(result)
        elif result["rotated"] or result["pruned"]:
            print(f"rotated: {result['rotated'] or '(none)'}; pruned: {', '.join(result['pruned']) or '(none)'}")
        else:
            print("Nothing to rotate.")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    events = query_events(
        paths.logs,
        agent=args.agent,
        type_filter=args.type_filter,
        since=args.since,
        status=args.status,
        limit=args.count,
    )
    if args.json_output:
        print_json(events)
    else:
        for event in events:
            print(format_event(event))
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    events = trace_events(paths.logs, args.identifier)
    if args.json_output:
        print_json(events)
    else:
        for event in events:
            print(format_event(event))
    return 0


def _cmd_cost(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    summary = cost_summary(paths.logs, since=args.since)
    rows = summary["agents"]
    if args.agent:
        rows = [row for row in rows if row["agent"] == args.agent]
    budgets = {}
    for row in rows:
        status = budget_status(paths.home, paths.logs, row["agent"])
        if status is not None:
            budgets[row["agent"]] = status
    if args.json_output:
        print_json({"agents": rows, "total": summary["total"], "budgets": budgets})
    else:
        _print_cost(rows, summary["total"], budgets)
    return 0


def _cmd_sessions(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.sessions_command == "list":
        sessions = [session.to_dict() for session in list_sessions(paths.sessions)]
        if args.json_output:
            print_json(sessions)
        else:
            for session in sessions:
                print(f"{session['id']}\t{session['status']}\t{session['backend']}\t{session['parent_agent_id']}\t{session['work_order'].get('goal')}")
    elif args.sessions_command == "inspect":
        print_json(read_session(paths.sessions, args.session_id).to_dict())
    elif args.sessions_command == "cancel":
        print_json(cancel_session(paths.sessions, args.session_id).to_dict())
    return 0


def _cmd_scheduler(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.scheduler_command == "due":
        at = datetime.fromisoformat(args.at) if args.at else None
        print_json(serialize_events(due_events(paths.agents, paths.workflows, now=at)))
    return 0


def _cmd_workspace(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.workspace_command == "gc":
        from jigga.runtime.deletion import gc_workspaces

        result = gc_workspaces(paths, apply=args.apply, protect=tuple(args.protect))
        if args.json_output:
            print_json(result)
            return 0
        orphans = result["orphans"]
        if not orphans:
            print("No orphaned workspaces — nothing to GC.")
            return 0
        if result["applied"]:
            print(f"Removed {len(result['removed'])} orphaned workspace(s) (backed up to state/backups/):")
            for wid in result["removed"]:
                print(f"  - {wid}")
        else:
            print(f"{len(orphans)} orphaned workspace(s) — re-run with --apply to back up + remove:")
            for orphan in orphans:
                print(f"  - {orphan['id']}  ({orphan['path']})")
        return 0
    return 0


def _cmd_team(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.team_command in ("get", "set"):
        from jigga.core.config import load_teams as _load_teams
        return _entity_get_set(args, entity_dir=paths.teams, entity_id=args.team_id,
                               verb=args.team_command,
                               validate_fn=lambda: _load_teams(paths.teams),
                               audit_type="team.changed", logs_dir=paths.logs)
    if args.team_command in ("disable", "enable"):
        if not (paths.teams / f"{args.team_id}.yaml").exists():
            print(f"! No such team: {args.team_id!r}")
            return 1
        return _toggle_disabled(paths, "teams", args.team_id,
                                disable=args.team_command == "disable",
                                json_output=args.json_output)
    if args.team_command == "delete":
        from jigga.runtime.deletion import delete_team
        try:
            result = delete_team(paths, args.team_id)
        except ValueError as exc:
            print(f"! {exc}")
            return 1
        if args.json_output:
            print_json(result)
        else:
            print(f"✓ Deleted team {args.team_id!r} — removed {len(result['removed'])} item(s)  "
                  "(backups: state/backups/)")
        return 0
    if args.team_command == "staff":
        from jigga.runtime.recipes import staff_member
        try:
            result = staff_member(paths, args.team_id, args.member_id, role_text=args.role_text)
        except ValueError as exc:
            print(f"! {exc}")
            return 1
        if args.json_output:
            print_json(result)
        else:
            print(f"✓ Staffed {args.member_id!r} on {args.team_id!r}")
            print(f"  recipe: {result['recipe']}  (now the team's source of truth)")
            print(f"  agent:  agents/{args.member_id}.yaml"
                  + ("" if result["agent_written"] else "  (already existed)"))
        return 0
    if args.team_command == "add-agent":
        try:
            result = add_agent_to_team(paths, args.team_id, args.agent_id, role=args.role,
                                       member_id=args.member_id, overwrite=args.overwrite)
        except ValueError as exc:
            if args.json_output:
                print_json({"error": str(exc)})
            else:
                print(f"! {exc}")
            return 1
        if args.json_output:
            print_json(result)
        else:
            print(f"✓ Added {args.agent_id!r} to {args.team_id!r} as member {result['member']!r} (role {result['role']!r})")
            print(f"  recipe: {result['recipe']}")
            print(f"  agent:  agents/{result['member']}.yaml"
                  + ("" if result["agent_written"] else "  (already existed)"))
        return 0
    if args.team_command in ("files", "file"):
        from jigga.runtime.entity_files import list_team_files
        return _entity_file_cmd(
            args, root=workspace_dir(paths.home, args.team_id),
            listing_fn=lambda: list_team_files(paths.home, args.team_id),
            entity=f"team:{args.team_id}", logs_dir=paths.logs)
    if args.team_command == "list":
        from jigga.runtime.workspaces import members, team_lead

        teams = [
            {"id": team.id, "name": team.name, "purpose": team.purpose,
             "lead": team_lead(team), "members": members(team)}
            for team in load_teams(paths.teams).values()
        ]
        teams.sort(key=lambda t: t["id"])
        if args.json_output:
            print_json(teams)
        elif not teams:
            print("No teams. Scaffold one: jigga recipes scaffold marketing-team")
        else:
            for team in teams:
                print(f"{team['id']:24} lead={team['lead'] or '-':20} members={len(team['members'])}  {team['name']}")
        return 0
    if args.team_command == "run":
        print_json(run_team(paths, args.team_id))
        return 0
    if args.team_command == "handoff":
        created = fire_handoffs(
            paths.home, paths.logs, paths.tasks, paths.teams,
            team_id=args.team_id, from_member=args.from_member,
            signal=args.signal, evidence=args.evidence,
        )
        if args.json_output:
            print_json(created)
        elif not created:
            print(f"No handoffs fired from {args.from_member!r} in team {args.team_id!r}.")
        else:
            for task in created:
                print(f"→ {task['assignee']}  (task {task['id']})  {task['title']}")
        return 0
    if args.team_command == "decisions":
        log = read_decision_log(paths.home, args.team_id)
        if args.json_output:
            print_json(log)
        elif not log:
            print(f"No handoffs recorded for team {args.team_id!r}.")
        else:
            for entry in log:
                when = f" on '{entry['when']}'" if entry.get("when") else ""
                print(f"{entry['time'][:19]}  {entry['from']} → {entry['to']}{when}  (task {entry['task_id']})")
        return 0
    if args.team_command == "lanes":
        from jigga.runtime.lanes import LaneError, team_lanes

        teams = load_teams(paths.teams)
        team = teams.get(args.team_id)
        if team is None:
            raise ValueError(f"Team not found: {args.team_id}")
        try:
            lanes = team_lanes(team)
        except LaneError as exc:
            if args.json_output:
                print_json({"error": str(exc)})
            else:
                print(f"! {exc}")
            return 1
        if args.json_output:
            print_json([{"id": lane.id, "description": lane.description, "gate": lane.gate} for lane in lanes])
        elif not lanes:
            print(f"Team {args.team_id!r} has no ticket lanes (add a `lanes:` block to its recipe/yaml).")
        else:
            for lane in lanes:
                gate = f"  [gate: {lane.gate}]" if lane.gate else ""
                print(f"{lane.id:16} {lane.description or ''}{gate}")
        return 0
    if args.team_command == "init":
        teams = load_teams(paths.teams)
        team = teams.get(args.team_id)
        if team is None:
            raise ValueError(f"Team not found: {args.team_id}")
        summary = scaffold_workspace(paths.home, team)
        if args.json_output:
            print_json(summary)
        else:
            print(f"Workspace: {summary['workspace']}")
            print(f"Lead/curator: {summary['lead']}")
            print(f"Members: {', '.join(summary['members']) or '(none)'}")
            print(f"Created: {', '.join(summary['created']) or '(already scaffolded)'}")
        return 0
    if args.team_command == "workspace":
        root = workspace_dir(paths.home, args.team_id)
        if not root.exists():
            print(f"No workspace for {args.team_id!r}. Run: jigga team init {args.team_id}")
            return 1
        print(str(root))
        for path in sorted(root.rglob("*")):
            if path.is_file():
                print(f"  {path.relative_to(root)}")
        return 0
    return 0


def _recipes_list(paths: Any, *, json_output: bool) -> int:
    # Full catalog, NO cross-dedup: every bundled template (always) + every
    # user-dir copy, each tagged `bundled`. The UI shows the complete builtin
    # catalog alongside your local copies; `source` is the unique key.
    recipes = list_recipe_files(paths.home)
    installed_ids = {rec.get("recipe_id") for rec in installed_recipes(paths.home)}
    for r in recipes:
        r["installed"] = r["id"] in installed_ids
    if json_output:
        print_json(recipes)
    elif not recipes:
        print("No recipes found.")
    else:
        for r in recipes:
            origin = "builtin" if r.get("bundled") else "local"
            marker = "installed" if r["installed"] else ""
            print(f"{r['id']:24} {r['kind']:6} {origin:8} {marker:10} {r.get('description') or ''}")
    return 0


def _recipes_installed(paths: Any, *, json_output: bool) -> int:
    records = installed_recipes(paths.home)
    if json_output:
        print_json(records)
        return 0
    if not records:
        print("No recipes installed. See what's available: jigga recipes list")
        return 0
    for rec in records:
        agents = [a for a in rec.get("artifacts", []) if a.startswith("agents/")]
        workflows = [a for a in rec.get("artifacts", []) if a.startswith("workflows/")]
        print(f"{rec['scaffold_id']:24} {rec.get('kind') or '?':6} "
              f"v{rec.get('version') or '?'}  installed {str(rec.get('installed_at') or '')[:10]}  "
              f"({len(agents)} agents, {len(workflows)} workflows)")
        for rel in rec.get("modified", []):
            print(f"  ~ locally edited: {rel}")
        for rel in rec.get("missing", []):
            print(f"  ! missing:        {rel}")
    return 0


def _recipes_scaffold(paths: Any, name: str, *, override_id: str | None,
                      overwrite: bool, json_output: bool) -> int:
    recipe_path = find_recipe(paths.home, name)
    if recipe_path is None:
        print(f"Recipe not found: {name!r}. List options with: jigga recipes list")
        return 1
    recipe = load_recipe(recipe_path)
    if recipe.kind == "agent":
        summary = scaffold_agent(paths.home, recipe, agent_id=override_id,
                                 overwrite=overwrite, agents_dir=paths.agents,
                                 workflows_dir=paths.workflows)
        if json_output:
            print_json(summary)
        else:
            print(f"Scaffolded agent {summary['agent_id']!r}"
                  + ("" if summary["written"] else "  (exists, skipped)")
                  + ("  [scheduled]" if summary["scheduled"] else ""))
            if summary.get("workflows_written"):
                print(f"  workflows: {', '.join(summary['workflows_written'])}")
            if summary.get("files_written"):
                print(f"  files:     {', '.join(summary['files_written'])}")
        return 0
    summary = scaffold_team(paths.home, recipe, team_id=override_id,
                            overwrite=overwrite, agents_dir=paths.agents, teams_dir=paths.teams,
                            workflows_dir=paths.workflows)
    if json_output:
        print_json(summary)
    else:
        print(f"Scaffolded team {summary['team_id']!r} (lead: {summary['lead']})")
        print(f"  team:      {summary['team_file']}{'' if summary['team_written'] else '  (exists, skipped)'}")
        print(f"  agents:    {', '.join(summary['agents_written']) or '(none new)'}"
              + (f"  | skipped: {', '.join(summary['agents_skipped'])}" if summary['agents_skipped'] else ""))
        if summary.get("workflows_written"):
            print(f"  workflows: {', '.join(summary['workflows_written'])}")
        print(f"  workspace: {summary['workspace']}")
        if summary.get("files_written"):
            print(f"  files:     {', '.join(summary['files_written'])}")
        print("Next: jigga team run " + summary['team_id'] + "   (or dispatch a task to the lead)")
    return 0


def _recipes_create(paths: Any, source: str, *, new_id: str, name: str | None,
                    overwrite: bool, json_output: bool) -> int:
    try:
        result = create_recipe_instance(paths.home, source, new_id=new_id, name=name,
                                        overwrite=overwrite, agents_dir=paths.agents,
                                        teams_dir=paths.teams, workflows_dir=paths.workflows)
    except ValueError as exc:
        if json_output:
            print_json({"error": str(exc)})
        else:
            print(f"! {exc}")
        return 1
    if json_output:
        print_json(result)
    else:
        scaffold = result["scaffold"]
        print(f"Created {result['kind']} {result['id']!r} from {source!r}")
        print(f"  recipe:    {result['recipe']}  (editable — now a Local recipe)")
        if result["kind"] == "agent":
            print(f"  agent:     {scaffold['agent_file']}")
        else:
            print(f"  team:      {scaffold['team_file']} (lead: {scaffold['lead']})")
            print(f"  agents:    {', '.join(scaffold['agents_written']) or '(none new)'}")
        print(f"  workspace: {scaffold['workspace']}")
    return 0


def _recipes_lint(paths: Any, name: str | None, *, json_output: bool) -> int:
    """Lint one recipe, or every recipe in the catalog. Exit 1 if any recipe has
    an error-level issue (or fails to load)."""
    if name:
        path = find_recipe(paths.home, name)
        targets = [(name, path)] if path else [(name, None)]
    else:
        targets = [(r["id"], Path(r["source"])) for r in list_recipe_files(paths.home)]

    report: list[dict[str, Any]] = []
    for label, path in targets:
        if path is None:
            report.append({"recipe": label, "source": None,
                           "issues": [{"level": "error", "code": "not_found", "message": "recipe not found"}]})
            continue
        try:
            issues = lint_recipe(load_recipe(path))
        except ValueError as exc:
            issues = [{"level": "error", "code": "load_failed", "message": str(exc)}]
        report.append({"recipe": label, "source": str(path), "issues": issues})

    has_error = any(i["level"] == "error" for entry in report for i in entry["issues"])
    if json_output:
        print_json(report)
        return 1 if has_error else 0

    flagged = [e for e in report if e["issues"]]
    if not flagged:
        print(f"✓ {len(report)} recipe(s) OK — no issues.")
        return 0
    for entry in flagged:
        print(f"{entry['recipe']}  ({entry['source']})")
        for issue in entry["issues"]:
            mark = "✗" if issue["level"] == "error" else "⚠"
            print(f"  {mark} [{issue['code']}] {issue['message']}")
    return 1 if has_error else 0



def _toggle_disabled(paths: Any, kind: str, entity_id: str, *, disable: bool,
                     json_output: bool) -> int:
    """Flip config `disabled.<agents|teams>` (operational state — entity yamls
    stay pristine). Audited."""
    from jigga.runtime.audit import append_event

    config = read_yaml(paths.config) if paths.config.exists() else {}
    bucket = config.setdefault("disabled", {}).setdefault(kind, [])
    present = entity_id in bucket
    if disable and not present:
        bucket.append(entity_id)
    elif not disable and present:
        bucket.remove(entity_id)
    write_yaml(paths.config, config)
    state = "disabled" if disable else "enabled"
    append_event(paths.logs, f"{kind[:-1]}.{state}", entity=entity_id)
    if json_output:
        print_json({"entity": entity_id, "kind": kind, "disabled": disable})
    else:
        print(f"✓ {entity_id} {state}"
              + ("" if disable else "") )
    return 0


def _entity_get_set(args: argparse.Namespace, *, entity_dir: Path, entity_id: str,
                    verb: str, validate_fn, audit_type: str, logs_dir: Path) -> int:
    """Dotted-key get/set over an entity yaml (team/agent) — the config_edit
    pattern applied to declared entities. `set` re-validates the whole file by
    reloading it; a value that breaks parsing is rolled back."""
    from jigga.runtime.audit import append_event
    from jigga.runtime.config_edit import coerce_value, get_path, set_path

    path = entity_dir / f"{entity_id}.yaml"
    if not path.exists():
        print(f"! No such entity: {entity_id!r}")
        return 1
    doc = read_yaml(path)
    if verb == "get":
        value = get_path(doc, args.key) if args.key else doc
        if args.json_output:
            print_json(value)
        else:
            import json as _json
            print(_json.dumps(value, indent=2, default=str) if isinstance(value, (dict, list)) else value)
        return 0
    value = coerce_value(args.value)
    try:
        old = set_path(doc, args.key, value)
    except ValueError as exc:
        print(f"! {exc}")
        return 1
    original = path.read_text(encoding="utf-8")
    write_yaml(path, doc)
    try:
        validate_fn()
    except Exception as exc:  # noqa: BLE001 — any load failure must roll back
        path.write_text(original, encoding="utf-8")
        print(f"! Change rolled back — it breaks {entity_id!r}: {exc}")
        return 1
    append_event(logs_dir, audit_type, entity=entity_id, key=args.key, old=old, new=value)
    if args.json_output:
        print_json({"entity": entity_id, "key": args.key, "old": old, "new": value})
    else:
        print(f"{entity_id}.{args.key}: {old!r} → {value!r}")
    return 0


def _entity_file_cmd(args: argparse.Namespace, *, root, listing_fn, entity: str, logs_dir) -> int:
    """Shared agents/team file surface: list, get, set — workspace-confined,
    audited writes (the jiggaview Files-tab backend)."""
    import sys as _sys

    from jigga.runtime.entity_files import read_entity_file, write_entity_file

    command = getattr(args, "agents_command", None) or getattr(args, "team_command", None)
    if command == "files":
        files = listing_fn()
        if args.json_output:
            print_json(files)
        else:
            for f in files:
                flags = ("required" if f["required"] else "optional") + (", missing" if f["missing"] else "")
                print(f"{f['name']:32} ({flags})")
        return 0
    try:
        if args.verb == "get":
            content = read_entity_file(root, args.name)
            if content is None:
                print(f"! {args.name} does not exist")
                return 1
            if args.json_output:
                print_json({"name": args.name, "content": content})
            else:
                print(content, end="")
            return 0
        content = args.content if args.content is not None else _sys.stdin.read()
        write_entity_file(root, args.name, content, logs_dir=logs_dir, entity=entity)
    except ValueError as exc:
        print(f"! {exc}")
        return 1
    if args.json_output:
        print_json({"name": args.name, "bytes": len(content.encode("utf-8"))})
    else:
        print(f"✓ wrote {args.name}")
    return 0


def _cmd_agents(args: argparse.Namespace) -> int:
    from jigga.runtime.workspaces import find_agent_teams

    paths = get_paths(args.home)
    if args.agents_command == "set" and getattr(args, "via_recipe", False):
        from jigga.runtime.config_edit import coerce_value
        from jigga.runtime.recipes import set_member_definition
        try:
            result = set_member_definition(paths, args.agent_id, args.key, coerce_value(args.value))
        except ValueError as exc:
            print(f"! {exc}")
            return 1
        if args.json_output:
            print_json(result)
        else:
            print(f"{args.agent_id}.{args.key}: {result['old']!r} → {result['new']!r}  "
                  f"(recipe: {result['recipe']})")
        return 0
    if args.agents_command in ("get", "set"):
        return _entity_get_set(args, entity_dir=paths.agents, entity_id=args.agent_id,
                               verb=args.agents_command,
                               validate_fn=lambda: load_agents(paths.agents),
                               audit_type="agent.changed", logs_dir=paths.logs)
    if args.agents_command in ("disable", "enable"):
        agents = load_agents(paths.agents)
        if args.agent_id not in agents:
            print(f"! No such agent: {args.agent_id!r}")
            return 1
        return _toggle_disabled(paths, "agents", args.agent_id,
                                disable=args.agents_command == "disable",
                                json_output=args.json_output)
    if args.agents_command == "delete":
        from jigga.runtime.deletion import delete_agent
        try:
            result = delete_agent(paths, args.agent_id)
        except ValueError as exc:
            print(f"! {exc}")
            return 1
        if args.json_output:
            print_json(result)
        else:
            print(f"✓ Deleted agent {args.agent_id!r}  (backups: state/backups/)")
            if result.get("destaffed_recipe"):
                print(f"  recipe member de-staffed (membership-only again): {result['destaffed_recipe']}")
        return 0
    if args.agents_command in ("files", "file"):
        from jigga.runtime.entity_files import agent_files_root, list_agent_files
        from jigga.runtime.workspaces import ensure_agent_workspace

        agent = load_agents(paths.agents).get(args.agent_id)
        if agent is None:
            print(f"! No such agent: {args.agent_id!r}")
            return 1
        ws = ensure_agent_workspace(paths.home, paths.teams, agent)
        return _entity_file_cmd(
            args, root=agent_files_root(paths.home, ws, args.agent_id),
            listing_fn=lambda: list_agent_files(paths.home, ws, args.agent_id),
            entity=f"agent:{args.agent_id}", logs_dir=paths.logs)
    agents = load_agents(paths.agents)
    out = []
    for agent in agents.values():
        teams = find_agent_teams(paths.teams, agent.id)
        out.append({
            "id": agent.id,
            "name": agent.name,
            "role": agent.role,
            "model": agent.model,
            "memory_scope": agent.memory_scope,
            "default": bool(agent.default),
            "tools": list(agent.tools or []),
            "team": teams[0].id if teams else None,
            "schedules": len((agent.wake or {}).get("schedules") or []),
        })
    out.sort(key=lambda a: (not a["default"], a["id"]))
    if args.json_output:
        print_json(out)
    elif not out:
        print("No agents. Scaffold some: jigga recipes scaffold personal-admin-team")
    else:
        for a in out:
            star = "*" if a["default"] else " "
            team = a["team"] or "-"
            print(f"{star} {a['id']:24} team={team:20} model={a['model'] or '-':16} "
                  f"tools={len(a['tools'])}  {a['role'][:50]}")
    return 0


def _cmd_recipes(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.recipes_command == "list":
        return _recipes_list(paths, json_output=args.json_output)
    if args.recipes_command == "show":
        recipe_path = find_recipe(paths.home, args.recipe)
        if recipe_path is None:
            print(f"Recipe not found: {args.recipe!r}. List options with: jigga recipes list")
            return 1
        summary = recipe_summary(load_recipe(recipe_path))
        if args.json_output:
            print_json(summary)
        else:
            print(f"{summary['id']} ({summary['kind']})  {summary.get('version') or ''}".rstrip())
            print(f"  {summary.get('description') or summary.get('purpose') or ''}".rstrip())
            print(f"  source: {summary['source']}")
            print("  agents:")
            for member in summary["agents"]:
                marker = "scaffolds" if member["scaffolded"] else "membership-only"
                req = "" if member.get("required", True) else ", optional"
                print(f"    {member['id']:32} {member['role']}  ({marker}{req})")
            if summary["workflows"]:
                print(f"  workflows: {', '.join(summary['workflows'])}")
            if summary["files"]:
                print(f"  files:     {', '.join(summary['files'])}")
        return 0
    if args.recipes_command == "scaffold":
        return _recipes_scaffold(paths, args.recipe, override_id=args.override_id,
                                 overwrite=args.overwrite, json_output=args.json_output)
    if args.recipes_command == "create":
        return _recipes_create(paths, args.recipe, new_id=args.new_id, name=args.name,
                               overwrite=args.overwrite, json_output=args.json_output)
    if args.recipes_command == "lint":
        return _recipes_lint(paths, args.recipe, json_output=args.json_output)
    if args.recipes_command == "delete":
        from jigga.runtime.audit import append_event
        from jigga.runtime.recipes import RECIPE_SUFFIX  # noqa: PLC0415

        stem = args.recipe.removesuffix(RECIPE_SUFFIX)
        user_copy = paths.home / "recipes" / f"{stem}{RECIPE_SUFFIX}"
        bundled = find_recipe(paths.home, stem) if not user_copy.exists() else None
        torn_down: list[str] = []
        if args.uninstall:
            from jigga.runtime.deletion import delete_agent, delete_team

            source_path = user_copy if user_copy.exists() else bundled
            if source_path is not None:
                recipe_id = load_recipe(source_path).id
                for record in installed_recipes(paths.home):
                    if record.get("recipe_id") != recipe_id:
                        continue
                    try:
                        if record.get("kind") == "agent":
                            delete_agent(paths, str(record["scaffold_id"]))
                        else:
                            delete_team(paths, str(record["scaffold_id"]))
                        torn_down.append(str(record["scaffold_id"]))
                    except ValueError as exc:
                        print(f"! {exc}")
        removed_copy = False
        if user_copy.exists():
            from jigga.runtime.update import backup_path_for
            from jigga.core.models import now_iso as _now

            backup = backup_path_for(paths.home, f"recipes/{stem}{RECIPE_SUFFIX}", stamp=_now()[:10])
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(user_copy.read_text(encoding="utf-8"), encoding="utf-8")
            user_copy.unlink()
            removed_copy = True
        elif not args.uninstall:
            print(f"! No user-dir copy of {stem!r} to delete"
                  + (" (it's a bundled recipe — shadow it with `recipes save` or use --uninstall)"
                     if bundled else ""))
            return 1
        append_event(paths.logs, "recipe.deleted", recipe=stem,
                     removed_copy=removed_copy, uninstalled=torn_down)
        if args.json_output:
            print_json({"recipe": stem, "removed_copy": removed_copy, "uninstalled": torn_down})
        else:
            if removed_copy:
                print(f"✓ removed user copy of {stem!r}"
                      + (" — bundled version takes over" if find_recipe(paths.home, stem) else ""))
            for entity in torn_down:
                print(f"✓ uninstalled {entity!r} (backups: state/backups/)")
        return 0
    if args.recipes_command == "cat":
        if args.bundled:
            # The bundled/default copy specifically, even when a user copy
            # shadows it — so the UI can diff "your installed recipe" vs "the
            # released default". (find_recipe resolves user-first.)
            from jigga.core.paths import examples_dir
            from jigga.runtime.recipes import RECIPE_SUFFIX
            stem = args.recipe.removesuffix(RECIPE_SUFFIX)
            candidate = examples_dir() / "recipes" / f"{stem}{RECIPE_SUFFIX}"
            recipe_path = candidate if candidate.exists() else None
        else:
            recipe_path = find_recipe(paths.home, args.recipe)
        if recipe_path is None:
            print(f"Recipe not found: {args.recipe!r}.")
            return 1
        print(recipe_path.read_text(encoding="utf-8"), end="")
        return 0
    if args.recipes_command == "save":
        import sys as _sys

        from jigga.runtime.audit import append_event
        from jigga.runtime.recipes import RECIPE_SUFFIX

        content = args.content if args.content is not None else _sys.stdin.read()
        stem = args.recipe.removesuffix(RECIPE_SUFFIX)
        if not stem or "/" in stem or stem.startswith("."):
            print(f"! Invalid recipe name: {args.recipe!r}")
            return 1
        target = paths.home / "recipes" / f"{stem}{RECIPE_SUFFIX}"
        target.parent.mkdir(parents=True, exist_ok=True)
        original = target.read_text(encoding="utf-8") if target.exists() else None
        target.write_text(content, encoding="utf-8")
        try:
            load_recipe(target)  # validate: bad frontmatter rolls back
        except ValueError as exc:
            if original is None:
                target.unlink(missing_ok=True)
            else:
                target.write_text(original, encoding="utf-8")
            print(f"! Not saved — invalid recipe: {exc}")
            return 1
        append_event(paths.logs, "recipe.saved", recipe=stem, path=str(target))
        if args.json_output:
            print_json({"recipe": stem, "path": str(target)})
        else:
            print(f"✓ saved {target} (user recipes override bundled — scaffold/update read this copy)")
        return 0
    if args.recipes_command == "installed":
        return _recipes_installed(paths, json_output=args.json_output)
    return 0



def _cmd_mailbox(args: argparse.Namespace) -> int:
    from jigga.runtime.audit import append_event
    from jigga.runtime.mailbox import list_messages, send_message
    from jigga.runtime.workspaces import find_agent_teams

    paths = get_paths(args.home)

    def _workspace_for(member: str) -> str:
        teams = find_agent_teams(paths.teams, member)
        return teams[0].id if teams else member

    if args.mailbox_command == "send":
        ws = _workspace_for(args.to)
        message = send_message(paths.home, ws, args.to, args.body,
                               sender=args.sender, subject=args.subject)
        append_event(paths.logs, "mailbox.sent", sender=args.sender, to=args.to,
                     workspace=ws, message_id=message["id"], subject=message.get("subject"))
        if args.json_output:
            print_json(message)
        else:
            print(f"Sent {message['id']} to {args.to!r} (workspace {ws}) — "
                  "they'll see it on their next wake.")
        return 0
    if args.mailbox_command == "list":
        ws = _workspace_for(args.member)
        messages = list_messages(paths.home, ws, args.member, unread_only=args.unread)
        if args.json_output:
            print_json(messages)
        elif not messages:
            print("Inbox empty." if not args.unread else "No unread messages.")
        else:
            for m in messages:
                flag = " " if m.get("read_at") else "*"
                subject = f"  {m['subject']}" if m.get("subject") else ""
                print(f"{flag} {m['id']}  {str(m.get('created_at') or '')[:16]}  "
                      f"from {m.get('from', '?')}{subject}")
                print(f"    {str(m.get('body') or '')[:120]}")
        return 0
    return 0


def _cmd_model(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.model_command == "test":
        agents = load_agents(paths.agents)
        agent = agents.get(args.agent_id)
        if agent is None:
            raise ValueError(f"Agent not found: {args.agent_id}")
        task = {"id": "model_test", "title": "Model test", "description": args.prompt}
        request = build_task_model_request(agent, task, dry_run=args.dry_run)
        print_json(call_model(paths.home, paths.logs, request).to_dict())
    elif args.model_command == "login":
        from jigga.runtime.chatgpt_login import browser_login, device_login
        result = device_login(paths.home) if args.device_code else browser_login(paths.home)
        print(f"\n✓ Logged in to ChatGPT subscription (account {result.get('account_id') or 'unknown'}).")
        print("  Set it as your provider with: jigga model use chatgpt")
    elif args.model_command == "status":
        from jigga.runtime.chatgpt_auth import login_state
        config = load_model_config(paths.home)
        print_json({
            "default_provider": (config.get("defaults") or {}).get("provider"),
            "providers": sorted((config.get("providers") or {}).keys()),
            "chatgpt_login": login_state(paths.home),
        })
    elif args.model_command == "use":
        _set_model_provider(paths, args.provider, args.model)
        print(f"Default model provider set to {args.provider!r}.")
        if args.provider == "chatgpt":
            print("If not logged in yet: jigga model login  (or --device-code)")
    elif args.model_command == "setup":
        _model_setup(paths)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    print_json(run_agent(paths.home, paths.logs, paths.tasks, paths.agents, args.agent_id, dry_run_model=args.dry_run_model))
    return 0


def _cmd_supervisor(args: argparse.Namespace) -> int:
    if args.supervisor_command == "tick":
        print_json(supervisor_tick(args.home))
    elif args.supervisor_command == "start":
        paths = get_paths(args.home)
        record_supervisor_start(paths.logs, args.interval_seconds, args.max_ticks)
        print_json(supervisor_loop(args.home, interval_seconds=args.interval_seconds, max_ticks=args.max_ticks,
                                   channel_long_poll_seconds=args.channel_poll_seconds))
    return 0


def _cmd_service(args: argparse.Namespace) -> int:
    from jigga.runtime.service import (
        install_service,
        start_service,
        status_service,
        stop_service,
        uninstall_service,
    )

    paths = get_paths(args.home)
    system = getattr(args, "system", False)
    if args.service_command == "install":
        result = install_service(paths, interval_seconds=args.interval_seconds, dry_run=args.dry_run, system=system)
        print_json(result)
        if result["backend"] == "unsupported":
            return 1
        if args.dry_run:
            return 0
        return 0 if result.get("started") else 1
    if args.service_command == "uninstall":
        print_json(uninstall_service(paths, system=system))
        return 0
    if args.service_command == "stop":
        result = stop_service(paths, system=system)
        print_json(result)
        return 0 if result.get("stopped") else 1
    if args.service_command == "start":
        result = start_service(paths, system=system)
        print_json(result)
        return 0 if result.get("started") else 1
    # status
    result = status_service(paths, system=system)
    print_json(result)
    return 0


def _cmd_task(args: argparse.Namespace) -> int:
    paths = get_paths(args.home)
    if args.task_command == "create":
        task = create_task(paths.tasks, args.title, args.description, args.assignee, args.workflow_id)
        print_json(task.to_dict())
    elif args.task_command == "list":
        tasks = [task.to_dict() for task in list_tasks(paths.tasks)]
        if getattr(args, "lane", None):
            tasks = [t for t in tasks if t.get("lane") == args.lane]
        if args.json_output:
            print_json(tasks)
        else:
            for task in tasks:
                lane = f"\t[{task['lane']}]" if task.get("lane") else ""
                print(f"{task['id']}\t{task['state']}\t{task.get('assignee') or '-'}{lane}\t{task['title']}")
    elif args.task_command == "set-state":
        print_json(set_task_state(paths.tasks, args.task_id, args.state).to_dict())
    elif args.task_command == "move":
        from jigga.runtime.lanes import LaneError, LaneGateError, move_task_lane

        try:
            task = move_task_lane(paths.home, paths.tasks, paths.logs, paths.teams,
                                  args.task_id, args.lane, actor=args.as_member)
        except (LaneError, LaneGateError) as exc:
            if args.json_output:
                print_json({"error": str(exc)})
            else:
                print(f"! {exc}")
            return 1
        if args.json_output:
            print_json(task.to_dict())
        else:
            print(f"Moved {task.id} → lane {task.lane!r}")
    return 0


def _cmd_skills(args: argparse.Namespace) -> int:
    from jigga.runtime.skills import create_skill, installed_skills, read_instructions, skill_records

    paths = get_paths(args.home)
    registry = CapabilityRegistry.load(
        user_capabilities=paths.capabilities,
        project_capabilities=project_capabilities_dir(resolve_project_root(args.project)),
        approvals_dir=paths.policies,
    )
    if args.skills_command == "list":
        records = skill_records(registry)
        pending = [c.name for c in registry.list_pending() if c.type == "skill_pack"]
        if args.json_output:
            print_json({"skills": records, "pending_approval": pending})
        else:
            for record in records:
                triggers = f"  triggers: {', '.join(record['triggers'])}" if record["triggers"] else ""
                print(f"- {record['name']} [{record['risk_level']}] — {record['summary']}{triggers}")
            for name in pending:
                print(f"- {name} (pending approval — jigga capabilities pending)")
            if not records and not pending:
                print("No skills installed. Create one: jigga skills create <name>")
    elif args.skills_command == "show":
        skill = next((s for s in installed_skills(registry) if s.name == args.name), None)
        if skill is None:
            raise ValueError(f"Skill not found: {args.name}")
        print_json(skill.to_dict())
        instructions = read_instructions(skill)
        if instructions:
            print("\n--- instructions ---\n" + instructions)
    elif args.skills_command == "create":
        print_json(create_skill(paths.capabilities, args.name, summary=args.summary))
    return 0


def _cmd_secrets(args: argparse.Namespace) -> int:
    import getpass

    from jigga.runtime.secrets_broker import delete_secret, list_secrets, set_secret

    paths = get_paths(args.home)
    if args.secrets_command == "set":
        value = getpass.getpass(f"Value for {args.name}: ")
        if not value:
            print("Empty value — nothing stored.")
            return 1
        print(f"Stored at {set_secret(paths.home, args.name, value)}")
    elif args.secrets_command == "list":
        for name in list_secrets(paths.home):
            print(f"- {name}")
    elif args.secrets_command == "delete":
        print("Deleted." if delete_secret(paths.home, args.name) else "Not found.")
    elif args.secrets_command == "migrate":
        from jigga.runtime.secrets_broker import migrate_to_encrypted

        migrated = migrate_to_encrypted(paths.home)
        print(f"Encrypted {len(migrated)} secret(s); backend set to encrypted-file. "
              "Set JIGGA_SECRETS_PASSPHRASE in the service unit before restarting the supervisor.")
    return 0


def _cmd_telemetry(args: argparse.Namespace) -> int:
    from jigga.runtime import telemetry

    paths = get_paths(args.home)
    if args.telemetry_command == "on":
        telemetry.set_enabled(paths.home, True)
        print("Telemetry ON. See exactly what gets sent: jigga telemetry report")
    elif args.telemetry_command == "off":
        telemetry.set_enabled(paths.home, False)
        print("Telemetry OFF.")
    elif args.telemetry_command == "status":
        print_json({"enabled": telemetry.enabled(paths.home),
                    "last_sent_at": telemetry._state(paths.home).get("last_sent_at"),
                    "endpoint": telemetry._config(paths.home).get("endpoint") or telemetry.DEFAULT_ENDPOINT})
    elif args.telemetry_command == "report":
        print_json(telemetry.build_payload(paths.home))
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    from pathlib import Path as _Path

    from jigga.runtime.backup import create_backup, inspect_backup, restore_backup

    paths = get_paths(args.home)
    if args.backup_command == "create":
        print_json(create_backup(paths.home, _Path(args.output) if args.output else None,
                                 include_secrets=args.include_secrets,
                                 encrypt=args.encrypt, recipient=args.recipient))
    elif args.backup_command == "inspect":
        print_json(inspect_backup(_Path(args.archive)))
    elif args.backup_command == "restore":
        print_json(restore_backup(_Path(args.archive), paths.home, force=args.force))
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "init": _cmd_init,
    "skills": _cmd_skills,
    "secrets": _cmd_secrets,
    "telemetry": _cmd_telemetry,
    "backup": _cmd_backup,
    "setup": _cmd_setup,
    "onboard": _cmd_onboard,
    "doctor": _cmd_doctor,
    "update": _cmd_update,
    "config": _cmd_config,
    "plugins": _cmd_plugins,
    "state": _cmd_state,
    "memory": _cmd_memory,
    "workflow": _cmd_workflow,
    "plan": _cmd_plan,
    "apply": _cmd_apply,
    "validate": _cmd_validate,
    "capabilities": _cmd_capabilities,
    "auth": _cmd_auth,
    "calendar": _cmd_calendar,
    "gog": _cmd_gog,
    "telegram": _cmd_telegram,
    "channels": _cmd_channels,
    "webchat": _cmd_webchat,
    "approvals": _cmd_approvals,
    "logs": _cmd_logs,
    "audit": _cmd_audit,
    "trace": _cmd_trace,
    "cost": _cmd_cost,
    "sessions": _cmd_sessions,
    "scheduler": _cmd_scheduler,
    "team": _cmd_team,
    "workspace": _cmd_workspace,
    "recipes": _cmd_recipes,
    "agents": _cmd_agents,
    "mailbox": _cmd_mailbox,
    "model": _cmd_model,
    "run": _cmd_run,
    "supervisor": _cmd_supervisor,
    "service": _cmd_service,
    "task": _cmd_task,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # IPv4-first resolution (config network.prefer_ipv4, default on) — a
    # blackholed IPv6 RA must degrade nothing (2026-07-29 incident).
    from jigga.runtime.netpref import install_ipv4_preference

    install_ipv4_preference(getattr(args, "home", None))
    handler = _COMMANDS.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args)
    except Exception as exc:  # noqa: BLE001 — top-level CLI error boundary
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from jigga.commands.init import init_runtime
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
from jigga.runtime.channel_listener import channel_listen, enabled_channels
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
from jigga.runtime.model_router import build_task_model_request, call_model, load_model_config
from jigga.core.io import read_yaml, write_yaml
from jigga.runtime.plan_apply import apply_runtime, plan_runtime, validate_runtime_configs
from jigga.runtime.daemon import record_supervisor_start, supervisor_loop
from jigga.runtime.scheduler import serialize_events, due_events
from jigga.runtime.subagents import cancel_session, list_sessions, read_session
from jigga.runtime.supervisor import supervisor_tick
from jigga.runtime.tasks import create_task, list_tasks, set_task_state
from jigga.runtime.team import run_team
from jigga.runtime.workspaces import scaffold_workspace, workspace_dir
from jigga.runtime.workflow import plan_workflow, run_workflow
from jigga.core.config import default_permission_mode, load_agents, load_teams, load_workflows


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
_CHANNEL_CATALOG: dict[str, tuple[str, str]] = {
    "telegram": ("telegram", "Telegram bot — poll inbound + reply"),
}


def _channels_setup(paths: Any, *, prompt: Any = input, echo: Any = print) -> None:
    """Interactive channel onboarding: pick a channel → run its guided install
    (auth + config + approval, reused from `capabilities install`) → set the
    activation mode → enable. Pluggable via `_CHANNEL_CATALOG`."""
    names = sorted(_CHANNEL_CATALOG)
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
    if install_capability(paths, capability, input_fn=prompt, print_fn=echo) != 0:
        return

    echo("\nActivation mode — when should the bot respond?")
    echo("  1) always              (every allowed message)")
    echo("  2) mention             (DMs always; groups only when @mentioned)")
    echo("  3) direct_message_only (only 1:1 chats)")
    echo("  4) disabled            (polled but inert)")
    mode = {"1": "always", "2": "mention", "3": "direct_message_only", "4": "disabled"}.get(
        prompt("> ").strip(), "always"
    )
    config = read_yaml(paths.config)
    entry = config.setdefault("channels", {}).setdefault(name, {})
    entry["enabled"] = True
    entry["activation"] = mode
    write_yaml(paths.config, config)
    echo(f"\n✓ {name} ready (activation={mode}). The supervisor polls it each tick — "
         "run `jigga supervisor run` (or install it as a service).")


def _model_setup(paths: Any, *, prompt: Any = input, echo: Any = print) -> None:
    """Interactive onboarding: pick a provider, then (for ChatGPT) authenticate."""
    from jigga.runtime.chatgpt_auth import login_state
    echo("Select a model provider:")
    echo("  1) ChatGPT subscription  (no API key, runs on your ChatGPT plan)")
    echo("  2) Dry-run               (no model; canned responses)")
    if prompt("> ").strip() in ("2", "dry_run", "dry-run"):
        _set_model_provider(paths, "dry_run", None)
        echo("Provider set to dry_run.")
        return
    _set_model_provider(paths, "chatgpt", None)
    if login_state(paths.home).get("logged_in"):
        echo("Provider set to chatgpt — an existing login was found. Done.")
        return
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
    init.add_argument("--examples", action="store_true", help="Copy bundled example agents and teams")
    init.add_argument(
        "--no-prompt",
        action="store_true",
        help="Skip the post-init optional-capability install prompt (also auto-skipped when stdin is not a TTY)",
    )

    state = sub.add_parser("state", help="Inspect local runtime state")
    state.add_argument("--json", action="store_true", dest="json_output")

    memory = sub.add_parser("memory", help="Inspect memory scopes and layers")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_sub.add_parser("inspect", help="Inspect configured memory scopes")

    workflow = sub.add_parser("workflow", help="Plan and run workflows")
    workflow_sub = workflow.add_subparsers(dest="workflow_command", required=True)
    workflow_plan = workflow_sub.add_parser("plan")
    workflow_plan.add_argument("workflow_id")
    workflow_plan.add_argument("--json", action="store_true", dest="json_output")
    workflow_run = workflow_sub.add_parser("run")
    workflow_run.add_argument("workflow_id")
    workflow_suggest = workflow_sub.add_parser("suggest")
    workflow_suggest.add_argument("--min-count", type=int, default=2)
    workflow_apply = workflow_sub.add_parser("apply")
    workflow_apply.add_argument("suggestion_id")
    workflow_apply.add_argument("--approve", action="store_true")

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

    team = sub.add_parser("team", help="Run team runtime skeleton")
    team_sub = team.add_subparsers(dest="team_command", required=True)
    team_run = team_sub.add_parser("run")
    team_init = team_sub.add_parser("init", help="Scaffold a team's shared workspace (notes/ + shared-context/ + roles/)")
    team_init.add_argument("team_id")
    team_init.add_argument("--json", action="store_true", dest="json_output")
    team_ws = team_sub.add_parser("workspace", help="Show a team's workspace path and files")
    team_ws.add_argument("team_id")
    team_run.add_argument("team_id")

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

    task = sub.add_parser("task", help="Manage local task queue")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_create = task_sub.add_parser("create")
    task_create.add_argument("--title", required=True)
    task_create.add_argument("--description")
    task_create.add_argument("--assignee")
    task_create.add_argument("--workflow", dest="workflow_id")
    task_list = task_sub.add_parser("list")
    task_list.add_argument("--json", action="store_true", dest="json_output")
    task_set = task_sub.add_parser("set-state")
    task_set.add_argument("task_id")
    task_set.add_argument("state")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            paths = init_runtime(args.home, examples=args.examples)
            print(f"Initialized JIGGA home: {paths.home}")
            if args.examples:
                print("Copied example agents and teams.")
            interactive = (not args.no_prompt) and sys.stdin.isatty() and sys.stdout.isatty()
            maybe_prompt_after_init(paths, interactive=interactive)
            return 0

        if args.command == "state":
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

        if args.command == "memory":
            paths = get_paths(args.home)
            if args.memory_command == "inspect":
                print_json(inspect_memory(paths.memory))
            return 0

        if args.command == "workflow":
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
                    for step in plan["steps"]:
                        reason = f": {step['policy']['reason']}" if step["policy"].get("reason") else ""
                        print(f"- {step['id']}: {step['action']} [{step['policy']['status']}{reason}]")
                    print("Plan: runnable" if plan["can_run"] else "Plan: blocked / approval needed")
            elif args.workflow_command == "run":
                print_json(
                    run_workflow(
                        paths.home,
                        paths.logs,
                        paths.workflows,
                        paths.agents,
                        paths.memory,
                        args.workflow_id,
                        project_capabilities=project_capabilities_dir(
                            resolve_project_root(args.project)
                        ),
                    )
                )
            elif args.workflow_command == "suggest":
                print_json(suggest_workflows(paths.logs, min_count=args.min_count))
            elif args.workflow_command == "apply":
                print_json(apply_suggestion(paths.workflows, args.suggestion_id, paths.logs, approve=args.approve))
            return 0

        if args.command == "plan":
            result = plan_runtime(get_paths(args.home))
            if args.json_output:
                print_json(result)
            else:
                print(f"Plan: {result['status']}")
                for change in result["changes"]:
                    approval = f" requires {change['requires_approval']}" if change.get("requires_approval") else ""
                    print(f"- {change['change']} {change['path']}{approval}")
            return 0

        if args.command == "apply":
            print_json(apply_runtime(get_paths(args.home), approve=args.approve))
            return 0

        if args.command == "validate":
            result = validate_runtime_configs(get_paths(args.home))
            if args.json_output:
                print_json(result)
            else:
                for kind, values in result.items():
                    print(f"{kind}: {', '.join(values) if values else 'none'}")
            return 0

        if args.command == "capabilities":
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

        if args.command == "auth":
            if args.auth_command == "status":
                print_json([status.to_dict() for status in auth_status()])
            elif args.auth_command == "login":
                exit_code = run_external_login(args.backend)
                return exit_code
            return 0

        if args.command == "calendar":
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

        if args.command == "gog":
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

        if args.command == "telegram":
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

        if args.command == "channels":
            paths = get_paths(args.home)
            if args.channels_command == "status":
                print_json(
                    [{"channel": name, "config": cfg} for name, cfg in enabled_channels(paths.home)]
                )
                return 0
            if args.channels_command == "setup":
                _channels_setup(paths)
                return 0
            return 0

        if args.command == "approvals":
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

        if args.command == "logs":
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

        if args.command == "audit":
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

        if args.command == "trace":
            paths = get_paths(args.home)
            events = trace_events(paths.logs, args.identifier)
            if args.json_output:
                print_json(events)
            else:
                for event in events:
                    print(format_event(event))
            return 0

        if args.command == "cost":
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

        if args.command == "sessions":
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

        if args.command == "scheduler":
            paths = get_paths(args.home)
            if args.scheduler_command == "due":
                at = datetime.fromisoformat(args.at) if args.at else None
                print_json(serialize_events(due_events(paths.agents, paths.workflows, now=at)))
            return 0

        if args.command == "team":
            paths = get_paths(args.home)
            if args.team_command == "run":
                print_json(run_team(paths.home, paths.logs, paths.tasks, paths.teams, paths.workflows, paths.agents, paths.memory, args.team_id))
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

        if args.command == "model":
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

        if args.command == "run":
            paths = get_paths(args.home)
            print_json(run_agent(paths.home, paths.logs, paths.tasks, paths.agents, args.agent_id, dry_run_model=args.dry_run_model))
            return 0

        if args.command == "supervisor":
            if args.supervisor_command == "tick":
                print_json(supervisor_tick(args.home))
            elif args.supervisor_command == "start":
                paths = get_paths(args.home)
                record_supervisor_start(paths.logs, args.interval_seconds, args.max_ticks)
                print_json(supervisor_loop(args.home, interval_seconds=args.interval_seconds, max_ticks=args.max_ticks))
            return 0

        if args.command == "task":
            paths = get_paths(args.home)
            if args.task_command == "create":
                task = create_task(paths.tasks, args.title, args.description, args.assignee, args.workflow_id)
                print_json(task.to_dict())
            elif args.task_command == "list":
                tasks = [task.to_dict() for task in list_tasks(paths.tasks)]
                if args.json_output:
                    print_json(tasks)
                else:
                    for task in tasks:
                        print(f"{task['id']}\t{task['state']}\t{task.get('assignee') or '-'}\t{task['title']}")
            elif args.task_command == "set-state":
                print_json(set_task_state(paths.tasks, args.task_id, args.state).to_dict())
            return 0

        parser.print_help()
        return 2
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

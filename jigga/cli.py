from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from jigga.commands.init import init_runtime
from jigga.commands.state import inspect_state
from jigga.core.paths import get_paths
from jigga.runtime.agent import run_agent
from jigga.runtime.inference import apply_suggestion, suggest_workflows
from jigga.runtime.memory import inspect_memory
from jigga.runtime.model_router import build_task_model_request, call_model
from jigga.runtime.plan_apply import apply_runtime, plan_runtime, validate_runtime_configs
from jigga.runtime.daemon import record_supervisor_start, supervisor_loop
from jigga.runtime.scheduler import serialize_events, due_events
from jigga.runtime.supervisor import supervisor_tick
from jigga.runtime.tasks import create_task, list_tasks, set_task_state
from jigga.runtime.team import run_team
from jigga.runtime.workflow import plan_workflow, run_workflow
from jigga.core.config import default_permission_mode, load_agents, load_workflows


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jigga", description="Local-first operating system for personal AI workers.")
    parser.add_argument("--home", type=Path, default=None, help="JIGGA home directory")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create a local runtime directory")
    init.add_argument("--examples", action="store_true", help="Copy bundled example agents and teams")

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

    scheduler = sub.add_parser("scheduler", help="Inspect scheduler due events")
    scheduler_sub = scheduler.add_subparsers(dest="scheduler_command", required=True)
    scheduler_due = scheduler_sub.add_parser("due", help="List due events for the current time")
    scheduler_due.add_argument("--at", help="Evaluate due events at an ISO timestamp")

    team = sub.add_parser("team", help="Run team runtime skeleton")
    team_sub = team.add_subparsers(dest="team_command", required=True)
    team_run = team_sub.add_parser("run")
    team_run.add_argument("team_id")

    model = sub.add_parser("model", help="Inspect and test model execution")
    model_sub = model.add_subparsers(dest="model_command", required=True)
    model_test = model_sub.add_parser("test", help="Run a model call for a configured agent")
    model_test.add_argument("agent_id")
    model_test.add_argument("--prompt", required=True)
    model_test.add_argument("--dry-run", action="store_true", help="Do not call external providers")

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
                print_json(run_workflow(paths.home, paths.logs, paths.workflows, paths.agents, paths.memory, args.workflow_id))
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

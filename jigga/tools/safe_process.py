from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, write_json
from jigga.core.models import AgentConfig, now_iso
from jigga.runtime.audit import new_id
from jigga.runtime.policy import evaluate_filesystem, evaluate_shell


class ProcessPolicyError(RuntimeError):
    pass


def run_safe_process(
    agent: AgentConfig,
    command: list[str],
    cwd: Path,
    artifacts_dir: Path,
    timeout_seconds: int = 30,
    apply: bool = False,
) -> dict[str, Any]:
    if not command:
        raise ValueError("command must not be empty")

    shell_command = " ".join(command)
    shell_decision = evaluate_shell(agent, shell_command)
    cwd_decision = evaluate_filesystem(agent, cwd, operation="execute")
    run_id = new_id("process")
    run_dir = artifacts_dir / run_id
    ensure_dir(run_dir)

    record: dict[str, Any] = {
        "id": run_id,
        "command": command,
        "cwd": str(cwd),
        "started_at": now_iso(),
        "policy": {"shell": shell_decision.to_dict(), "cwd": cwd_decision.to_dict()},
        "dry_run": not apply,
    }

    blocked = [decision for decision in (shell_decision, cwd_decision) if decision.status == "deny"]
    approvals = [decision for decision in (shell_decision, cwd_decision) if decision.status == "ask"]
    if blocked:
        record["status"] = "denied"
        record["completed_at"] = now_iso()
        write_json(run_dir / "process.json", record)
        raise ProcessPolicyError(blocked[0].reason or "Process denied by policy.")
    if approvals or not apply:
        record["status"] = "needs_approval" if approvals else "planned"
        record["completed_at"] = now_iso()
        write_json(run_dir / "process.json", record)
        return record

    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    (run_dir / "stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    record.update(
        {
            "status": "completed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "stdout": str(run_dir / "stdout.txt"),
            "stderr": str(run_dir / "stderr.txt"),
            "completed_at": now_iso(),
        }
    )
    write_json(run_dir / "process.json", record)
    return record

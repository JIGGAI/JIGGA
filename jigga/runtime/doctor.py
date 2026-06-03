"""`jigga doctor` — one post-install health screen.

A fresh user shouldn't have to know that model state lives behind `model
status`, backend availability behind `auth status`, channels behind `channels
status`, config problems behind `validate`, and service state behind `service
status`. Doctor folds those scattered checks into a single ✓/⚠/✗ report and a
meaningful exit code (non-zero only on a real *failure* — a broken runtime or a
config error — not on the many optional-but-unconfigured warnings).

This is report-only today; the structure (each check carries a remediation
`hint`) is what a future `--fix` would act on, mirroring OpenClaw's
`doctor --repair`.

Checks are individually small and reach into the existing status helpers, so
they stay in sync with the real commands rather than duplicating their logic.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

from jigga.core.paths import JiggaPaths

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str | None = None

    def to_dict(self) -> dict:
        d = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.hint:
            d["hint"] = self.hint
        return d


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return any(c.status == FAIL for c in self.checks)

    @property
    def warned(self) -> bool:
        return any(c.status == WARN for c in self.checks)

    def to_dict(self) -> dict:
        return {
            "ok": not self.failed,
            "checks": [c.to_dict() for c in self.checks],
            "summary": {s: sum(1 for c in self.checks if c.status == s) for s in (OK, WARN, FAIL)},
        }


def _check_python() -> Check:
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) >= (3, 11):
        return Check("python", OK, f"Python {version}")
    return Check("python", FAIL, f"Python {version} is below the required 3.11",
                 hint="Recreate the venv with a 3.11+ interpreter (see scripts/install.sh).")


def _check_runtime(paths: JiggaPaths) -> Check:
    if not paths.home.exists():
        return Check("runtime", FAIL, f"No runtime at {paths.home}",
                     hint="Run `jigga init` (or `jigga onboard`).")
    missing = [name for name, p in (("agents", paths.agents), ("memory", paths.memory),
                                    ("logs", paths.logs), ("tasks", paths.tasks))
               if not p.exists()]
    if missing:
        return Check("runtime", FAIL, f"Runtime at {paths.home} missing dirs: {', '.join(missing)}",
                     hint="Run `jigga init` to repair the runtime layout.")
    return Check("runtime", OK, f"Runtime at {paths.home}")


def _check_config(paths: JiggaPaths) -> Check:
    from jigga.core.config import load_agents, load_teams
    from jigga.runtime.validation import is_error, validate_configs

    try:
        agents = load_agents(paths.agents)
        teams = load_teams(paths.teams)
    except Exception as exc:  # noqa: BLE001 — surface a load failure as a check, don't crash doctor
        return Check("config", FAIL, f"Could not load configs: {exc}")
    problems = validate_configs(agents, teams)
    errors = [p for p in problems if is_error(p)]
    warns = [p for p in problems if not is_error(p)]
    n = len(agents)
    if errors:
        return Check("config", FAIL, f"{len(errors)} config error(s): " + "; ".join(errors[:3]),
                     hint="Run `jigga validate` for the full list.")
    if warns:
        return Check("config", WARN, f"{n} agents valid, {len(warns)} warning(s): " + "; ".join(warns[:3]),
                     hint="Run `jigga validate` for details.")
    return Check("config", OK, f"{n} agents, {len(teams)} teams — all valid")


def _check_default_agent(paths: JiggaPaths) -> Check:
    from jigga.core.config import resolve_default_agent

    agent_id = resolve_default_agent(paths.agents)
    if agent_id:
        return Check("default_agent", OK, f"Default agent: {agent_id}")
    return Check("default_agent", WARN, "No default agent set",
                 hint="Run `jigga setup` (or `jigga onboard`) to scaffold one.")


def _check_model(paths: JiggaPaths) -> Check:
    from jigga.runtime.chatgpt_auth import login_state
    from jigga.runtime.model_router import load_model_config

    try:
        cfg = load_model_config(paths.home)
    except Exception:  # noqa: BLE001
        cfg = {}
    provider = (cfg.get("defaults") or {}).get("provider")
    if not provider:
        return Check("model", WARN, "No model provider configured (agents run on the dry-run provider)",
                     hint="Run `jigga model setup`.")
    if provider == "chatgpt" and not login_state(paths.home).get("logged_in"):
        return Check("model", WARN, "Provider is 'chatgpt' but not logged in",
                     hint="Run `jigga model login`.")
    return Check("model", OK, f"Model provider: {provider}")


def _check_channels(paths: JiggaPaths) -> Check:
    from jigga.runtime.channel_listener import enabled_channels

    try:
        channels = enabled_channels(paths.home)
    except Exception:  # noqa: BLE001
        channels = []
    if not channels:
        return Check("channels", WARN, "No chat channels enabled",
                     hint="Run `jigga channels setup` (optional).")
    names = ", ".join(name for name, _ in channels)

    # Reply-loop check: a channel's routed agent can only respond if it (a) holds
    # the channel's send tool AND (b) is permitted to reach the channel's network
    # host. Missing either => inbound messages complete silently.
    from jigga.core.config import load_agents, resolve_default_agent
    from jigga.runtime.capabilities import CapabilityRegistry
    from jigga.runtime.policy import evaluate_network

    try:
        agents = load_agents(paths.agents)
        default_id = resolve_default_agent(paths.agents)
        registry = CapabilityRegistry.load(user_capabilities=paths.capabilities, approvals_dir=paths.policies)
    except Exception:  # noqa: BLE001
        agents, default_id, registry = {}, None, None
    cant_reply = []
    for name, cfg in channels:
        routed_id = (cfg or {}).get("default_agent") or default_id
        agent = agents.get(routed_id) if routed_id else None
        if agent is None:
            cant_reply.append(f"{name} routes to missing agent '{routed_id}'")
            continue
        if f"{name}.send_message" not in (agent.tools or []):
            cant_reply.append(f"{routed_id} lacks {name}.send_message")
            continue
        capability = registry.get(name) if registry else None
        net = (capability.permissions or {}).get("network") if capability else None
        target = net.get("target") if isinstance(net, dict) else None
        if target and evaluate_network(agent, target).status != "allow":
            cant_reply.append(f"{routed_id} can't reach {target}")
    if cant_reply:
        return Check("channels", WARN, f"Enabled: {names}; but replies won't send: {'; '.join(cant_reply)}",
                     hint="Re-run `jigga channels setup` to grant the channel's tools + network egress.")
    return Check("channels", OK, f"Enabled channels: {names} (agent can reply)")


def _check_backends() -> Check:
    from jigga.runtime.auth import auth_status

    statuses = auth_status()
    available = [s.backend for s in statuses if s.binary_available]
    missing = [s.backend for s in statuses if not s.binary_available]
    if available:
        detail = f"Subagent CLIs on PATH: {', '.join(available)}"
        if missing:
            detail += f" (missing: {', '.join(missing)})"
        return Check("backends", OK, detail)
    return Check("backends", WARN, f"No subagent CLI backends on PATH (missing: {', '.join(missing)})",
                 hint="Install codex/claude and run `jigga auth login <backend>` (optional).")


def _check_service(paths: JiggaPaths) -> Check:
    from jigga.runtime.service import status_service

    st = status_service(paths)
    if st["backend"] == "unsupported":
        return Check("service", WARN, "No user service manager on this platform",
                     hint="Run `jigga supervisor start` to keep it running yourself.")
    if not st.get("installed"):
        return Check("service", WARN, "Supervisor not installed as a service (won't survive reboot)",
                     hint="Run `jigga service install` (or `jigga onboard --install-daemon`).")
    if st.get("running"):
        return Check("service", OK, f"Supervisor service installed and running ({st['backend']})")
    return Check("service", WARN, f"Supervisor service installed but not running ({st['backend']})",
                 hint="Check `jigga service status`.")


def run_checks(paths: JiggaPaths) -> Report:
    """Run all health checks against the runtime and return the aggregated report."""
    report = Report()
    report.checks.append(_check_python())
    report.checks.append(_check_runtime(paths))
    # The runtime-dependent checks only make sense once the home exists.
    if paths.home.exists():
        report.checks.append(_check_config(paths))
        report.checks.append(_check_default_agent(paths))
        report.checks.append(_check_model(paths))
        report.checks.append(_check_channels(paths))
        report.checks.append(_check_service(paths))
    report.checks.append(_check_backends())
    return report

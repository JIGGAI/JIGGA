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


PROBE_PROMPT = "Reply with the single word: ok"


def _probe_model(paths: JiggaPaths, provider: str) -> Check:
    """Send one real inference through the configured provider.

    Credentials existing is not evidence that the model path works. On the
    precursor stack a valid `OPENAI_API_KEY` sat next to a dead Codex OAuth
    refresh token — the provider authenticated through a different path
    entirely, so `curl /v1/models` returning 200 proved nothing. The only
    symptom was `ToolsInvokeError (errorCategory: "unknown")` surfacing days
    later inside a workflow run, with the real cause in a log the service was
    sending to /dev/null. It cost days and produced a confident wrong diagnosis
    that persisted for weeks.

    The only check that proves the model path is alive is using it, so this
    goes through `call_model` — the same entry point a workflow node uses —
    rather than a synthetic credential test.
    """
    from jigga.runtime.model_router import ModelCallItem, ModelCallRequest, call_model

    request = ModelCallRequest(
        agent_id="doctor",
        role="diagnostic",
        task={"id": "doctor_probe", "title": "provider probe", "description": PROBE_PROMPT},
        items=[ModelCallItem(id="doctor_probe", role="user", content=PROBE_PROMPT)],
        dry_run=False,
    )
    hint = ("This is the real error off the model path, not a credential check. CLI/OAuth-backed "
            "providers expire independently of any API key — try `jigga model status`, then "
            "`jigga model login`.")
    try:
        result = call_model(paths.home, paths.logs, request)
    except Exception as exc:  # noqa: BLE001 — the point of a probe is to report the fault, not raise it
        return Check("model", FAIL,
                     f"Provider {provider!r} probe raised {type(exc).__name__}: {exc}"[:400], hint=hint)
    if result.status != "ok":
        return Check("model", FAIL, f"Provider {provider!r} probe failed: {result.error}"[:400], hint=hint)
    return Check("model", OK, f"Model provider: {provider} — probe returned a live response")


def _check_model(paths: JiggaPaths, *, probe: bool = False) -> Check:
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
    # `dry_run` is what `jigga init` writes, and it answers every request
    # successfully — so probing it would report a live model path on a runtime
    # that cannot think at all. Never probe it, and never call it OK.
    if provider == "dry_run":
        return Check("model", WARN, "Model provider is 'dry_run' — agents return canned text, not model output",
                     hint="Run `jigga model setup` to configure a real provider.")
    if provider == "chatgpt" and not login_state(paths.home).get("logged_in"):
        return Check("model", WARN, "Provider is 'chatgpt' but not logged in",
                     hint="Run `jigga model login`.")
    if probe:
        return _probe_model(paths, provider)
    # Deliberately does not claim the provider works — that's the failure this
    # check used to have. Configured and working are different states.
    return Check("model", OK, f"Model provider: {provider} (configured; not probed)",
                 hint="Credentials existing doesn't prove the model path works — "
                      "`jigga doctor --probe` sends one real request through it.")


def _check_agent_tools(paths: JiggaPaths) -> Check:
    """Grants that can't work: an action naming no capability, or one whose
    capability needs a permission the agent doesn't have.

    Both fail quietly. An unregistered action is filtered out before the model
    is ever offered it, so the agent simply never does that thing and nobody
    learns why; a blocked one is offered and fails at the moment of use.
    """
    from jigga.core.config import load_agents
    from jigga.runtime.capabilities import CapabilityRegistry
    from jigga.runtime.dispatcher import unusable_grants

    try:
        agents = load_agents(paths.agents)
        registry = CapabilityRegistry.load(user_capabilities=paths.capabilities,
                                           approvals_dir=paths.policies)
    except Exception:  # noqa: BLE001
        return Check("agent_tools", WARN, "Could not load agents to check their grants")
    if not agents:
        return Check("agent_tools", OK, "No agents configured")

    problems: list[str] = []
    for agent_id, agent in sorted(agents.items()):
        for row in unusable_grants(agent, registry):
            label = "unknown action" if row["status"] == "unregistered" else "no permission"
            problems.append(f"{agent_id}:{row['action']} ({label})")
    if not problems:
        return Check("agent_tools", OK, f"All granted tools usable across {len(agents)} agent(s)")
    shown = "; ".join(problems[:4]) + (f"; +{len(problems) - 4} more" if len(problems) > 4 else "")
    return Check("agent_tools", WARN, f"{len(problems)} grant(s) can't work: {shown}",
                 hint="`jigga agents tools <id>` shows the full picture per agent.")


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

    # iMessage is the one channel that can be enabled on a machine that cannot
    # possibly run it — and then silently polls nothing forever. Checked before
    # the reply-loop below because "can't run at all" outranks "can't reply".
    if any(name == "imessage" for name, _ in channels):
        from jigga.runtime.imessage import availability

        state = availability(paths.home)
        if not state["available"]:
            return Check("channels", WARN,
                         f"Enabled: {names}; but iMessage can't run here — {state['reason']}",
                         hint="Disable it in config, or run JIGGA on the Mac signed in to Messages.")

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


def _check_secrets(paths: JiggaPaths) -> Check:
    from jigga.runtime.secrets_broker import list_secrets, resolved_backend

    backend = resolved_backend(paths.home)
    count = len(list_secrets(paths.home))
    return Check("secrets", OK, f"Secrets backend: {backend} ({count} stored)")


def _check_egress(paths: JiggaPaths) -> Check:
    """E3b: observed-vs-declared — blocked egress attempts mean a capability
    tried to reach hosts outside its manifest's allowlist. That's either a
    missing declaration (fix the manifest) or the exact behavior the proxy
    exists to stop; both deserve eyes."""
    import json as _json

    log = paths.logs / "events.jsonl"
    if not log.exists():
        return Check("egress", OK, "No egress activity recorded")
    blocked: dict[str, set[str]] = {}
    try:
        lines = log.read_text(encoding="utf-8").splitlines()[-5000:]
    except OSError:
        return Check("egress", OK, "No egress activity recorded")
    for line in lines:
        try:
            event = _json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "egress.blocked":
            details = event.get("details") or {}
            blocked.setdefault(str(details.get("label")), set()).add(str(details.get("host")))
    if not blocked:
        return Check("egress", OK, "No blocked egress attempts in the recent audit log")
    summary = "; ".join(f"{label} → {', '.join(sorted(hosts))}" for label, hosts in sorted(blocked.items()))
    return Check("egress", WARN, f"Blocked egress attempts (observed vs declared): {summary}",
                 hint="If legitimate, add the host to that capability's permissions.network.allow; "
                      "otherwise the capability is trying to reach hosts it never declared.")


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
    # A unit installed in BOTH scopes is the invisible failure: only one wins
    # the port/lock race, the obvious check (`launchctl list`) shows the user
    # domain only, and the two can carry different env — so which one won
    # silently decided whether magic-link URLs and the inbound webhook worked.
    # On the precursor stack the loser respawned every 5s into a 124MB log.
    try:
        system_st = status_service(paths, system=True)
    except Exception:  # noqa: BLE001 — probing the system scope must never break the check
        system_st = {}
    if st.get("installed") and system_st.get("installed"):
        return Check("service", WARN,
                     f"Supervisor unit installed in BOTH user and system scope ({st['backend']})",
                     hint="Two supervisors race for the same resources and may carry different env; "
                          f"only one wins and the other respawns. Remove one — "
                          f"user: {st.get('unit_path')} / system: {system_st.get('unit_path')}")
    if not st.get("installed") and system_st.get("installed"):
        return Check("service", OK,
                     f"Supervisor service installed at system scope ({st['backend']})")
    if not st.get("installed"):
        return Check("service", WARN, "Supervisor not installed as a service (won't survive reboot)",
                     hint="Run `jigga service install` (or `jigga onboard --install-daemon`).")
    if st.get("running"):
        return Check("service", OK, f"Supervisor service installed and running ({st['backend']})")
    return Check("service", WARN, f"Supervisor service installed but not running ({st['backend']})",
                 hint="Check `jigga service status`.")


def _fix_runtime(paths: JiggaPaths, _check: Check) -> str | None:
    from jigga.commands.init import init_runtime

    init_runtime(paths.home)  # idempotent: recreates the dir layout, never clobbers
    return "Repaired the runtime layout (jigga init)."


def _fix_service(paths: JiggaPaths, check: Check) -> str | None:
    from jigga.runtime.service import detect_backend, install_service, start_service

    if detect_backend() == "unsupported":
        return None
    if "not running" in check.detail:
        res = start_service(paths)
        return f"Restarted the supervisor service ({res['backend']})." if res.get("started") else None
    # not installed (won't survive reboot)
    res = install_service(paths)
    return f"Installed the supervisor service ({res['backend']})." if res.get("started") else None


# check name → a SAFE, non-interactive remediation. Only structural/operational
# fixes belong here — config errors, model login, channel setup etc. need the
# user, so they stay hint-only.
_FIXERS = {
    "runtime": _fix_runtime,
    "service": _fix_service,
}


def run_fixes(paths: JiggaPaths, report: Report) -> list[dict]:
    """Apply the safe auto-fix for each fixable non-OK check. Returns
    `[{check, fixed, message}]`. Mirrors OpenClaw's `doctor --repair`."""
    actions: list[dict] = []
    for check in report.checks:
        if check.status == OK:
            continue
        fixer = _FIXERS.get(check.name)
        if fixer is None:
            continue
        try:
            message = fixer(paths, check)
        except Exception as exc:  # noqa: BLE001 — a failed fix is reported, never fatal
            actions.append({"check": check.name, "fixed": False, "message": f"fix failed: {exc}"})
            continue
        if message:
            actions.append({"check": check.name, "fixed": True, "message": message})
    return actions


def run_checks(paths: JiggaPaths, *, probe: bool = False) -> Report:
    """Run all health checks against the runtime and return the aggregated report.

    `probe` opts into the checks that spend real resources — today that's one
    live model request. It defaults False so importing callers and tests stay
    offline; the `doctor` CLI turns it on unless `--no-probe` is passed.
    """
    report = Report()
    report.checks.append(_check_python())
    report.checks.append(_check_runtime(paths))
    # The runtime-dependent checks only make sense once the home exists.
    if paths.home.exists():
        report.checks.append(_check_config(paths))
        report.checks.append(_check_default_agent(paths))
        report.checks.append(_check_model(paths, probe=probe))
        report.checks.append(_check_agent_tools(paths))
        report.checks.append(_check_channels(paths))
        report.checks.append(_check_service(paths))
        report.checks.append(_check_secrets(paths))
        report.checks.append(_check_egress(paths))
    report.checks.append(_check_backends())
    return report

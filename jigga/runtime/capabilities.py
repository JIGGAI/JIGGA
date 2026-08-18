from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, list_config_files, read_json, read_yaml, write_json

APPROVALS_FILE = "capability_approvals.json"

VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_CAPABILITY_TYPES = {"native", "skill_pack", "mcp_server", "app"}

# Auto-assigned handler key for each capability type when the manifest does
# not declare one explicitly. Built-in dispatch table registers each of these.
DEFAULT_HANDLERS_BY_TYPE = {
    "app": "app.none",  # apps are supervised processes, never dispatched as actions
    "native": "dry_run.generic",
    "skill_pack": "skill_pack.default",
    "mcp_server": "mcp_server.subprocess",
}


@dataclass(frozen=True)
class CapabilityManifest:
    name: str
    version: str
    summary: str
    actions: list[str]
    type: str = "native"
    triggers: list[str] = field(default_factory=list)
    requires: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    risk_level: str = "low"
    handler: str = "dry_run.generic"
    # mcp_server-specific fields. Ignored for other types.
    command: str | None = None
    args: list[str] = field(default_factory=list)
    transport: str = "stdio"
    # skill_pack-specific field: filename inside the pack dir holding the
    # instructions/system-prompt the model is given on dispatch.
    instructions: str = "instructions.md"
    # Per-action input shapes: {action: {param: {type, description, required?}}}.
    # Optional and additive — an action without one is still offered with an
    # open object, exactly as before.
    #
    # Every action used to be advertised as `{"properties": {}}`, i.e. "takes
    # anything", so the model had to infer what a tool wanted from its one-line
    # summary. Declaring the shape is the difference between calling a tool and
    # guessing at it.
    action_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    # app-specific fields (plugins — out-of-process supervised sidecars):
    # `run` is the long-running argv (service ExecStart, cwd = plugin dir);
    # `setup` is a list of one-shot argvs run at install (npm ci, build...);
    # `port`/`app_env` flow into the service unit environment.
    run: list[str] = field(default_factory=list)
    setup: list[list[str]] = field(default_factory=list)
    port: int | None = None
    app_env: dict[str, str] = field(default_factory=dict)
    source: str | None = None
    manifest_hash: str | None = None
    bundled: bool = False
    # Routing guidance appended to the action's function-schema description —
    # the ONLY signal the model routes tools/skills by. Write it as trigger
    # language: "When asked for an outline or talking points — not full drafts."
    when_to_use: str | None = None
    # Actions only the RUNTIME may invoke — never granted to or callable by an
    # agent. Channel ingest (telegram.poll_messages) lives here: Telegram's
    # getUpdates allows ONE consumer per bot token, and an agent polling would
    # collide with the supervisor's long-poll or steal the update offset
    # (silently eating inbound messages).
    runtime_only_actions: list[str] = field(default_factory=list)

    def is_runtime_only(self, action: str) -> bool:
        return action in (self.runtime_only_actions or [])

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        source: str | None = None,
        bundled: bool = False,
        manifest_hash: str | None = None,
    ) -> "CapabilityManifest":
        kind_early = str(data.get("type", "native"))
        required = ("name", "version", "summary") if kind_early == "app" else ("name", "version", "summary", "actions")
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise ValueError(f"Capability manifest missing required fields: {', '.join(missing)}")
        actions = data.get("actions") or []
        if kind_early == "app":
            if actions:
                raise ValueError("type: app manifests declare no actions — apps are supervised "
                                 "processes, not dispatchable tools")
        elif not isinstance(actions, list) or not actions or not all(isinstance(item, str) and item for item in actions):
            raise ValueError("Capability manifest field 'actions' must be a non-empty list of strings")
        risk = str(data.get("risk_level", "low"))
        if risk not in VALID_RISK_LEVELS:
            raise ValueError(f"Invalid capability risk_level: {risk!r}")
        kind = str(data.get("type", "native"))
        if kind not in VALID_CAPABILITY_TYPES:
            raise ValueError(
                f"Invalid capability type: {kind!r}. "
                f"Allowed: {', '.join(sorted(VALID_CAPABILITY_TYPES))}."
            )

        # Per-type required-field validation. Keep loose where the doc is loose
        # — skill_pack's `instructions` defaults to `instructions.md` and the
        # handler reports a clear error if the file is missing at dispatch.
        command = data.get("command")
        if kind == "mcp_server":
            if not command or not isinstance(command, str):
                raise ValueError("mcp_server capability requires a non-empty 'command' string")

        args = data.get("args") or []
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ValueError("Capability 'args' must be a list of strings")

        run = data.get("run") or []
        if kind == "app":
            if not isinstance(run, list) or not run or not all(isinstance(item, str) and item for item in run):
                raise ValueError("type: app capability requires a non-empty 'run' argv list")
        raw_setup = data.get("setup") or []
        setup: list[list[str]] = []
        for entry in raw_setup:
            if not isinstance(entry, list) or not all(isinstance(item, str) for item in entry):
                raise ValueError("Capability 'setup' must be a list of argv lists")
            setup.append([str(item) for item in entry])
        port = data.get("port")
        if port is not None and not isinstance(port, int):
            raise ValueError("Capability 'port' must be an integer")

        # Handler defaults are type-aware. If the manifest declares a handler
        # explicitly, that wins (so native packs can still ship custom code via
        # dotted import paths). Otherwise the type picks a sensible default.
        handler = data.get("handler")
        if not handler:
            handler = DEFAULT_HANDLERS_BY_TYPE[kind]

        return cls(
            name=str(data["name"]),
            version=str(data["version"]),
            summary=str(data["summary"]),
            actions=list(actions),
            type=kind,
            triggers=list(data.get("triggers") or []),
            requires=dict(data.get("requires") or {}),
            permissions=dict(data.get("permissions") or {}),
            risk_level=risk,
            handler=str(handler),
            command=str(command) if command else None,
            args=[str(item) for item in args],
            transport=str(data.get("transport", "stdio")),
            instructions=str(data.get("instructions", "instructions.md")),
            action_inputs={str(k): dict(v) for k, v in (data.get("action_inputs") or {}).items()
                           if isinstance(v, dict)},
            source=source,
            manifest_hash=manifest_hash,
            bundled=bundled,
            runtime_only_actions=[str(a) for a in (data.get("runtime_only_actions") or [])],
            when_to_use=str(data["when_to_use"]) if data.get("when_to_use") else None,
            run=[str(item) for item in run] if isinstance(run, list) else [],
            setup=setup,
            port=port,
            app_env={str(k): str(v) for k, v in (data.get("app_env") or {}).items()},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "summary": self.summary,
            "actions": self.actions,
            "type": self.type,
            "triggers": self.triggers,
            "requires": self.requires,
            "permissions": self.permissions,
            "risk_level": self.risk_level,
            "handler": self.handler,
            "command": self.command,
            "args": self.args,
            "transport": self.transport,
            "instructions": self.instructions,
            "source": self.source,
            "manifest_hash": self.manifest_hash,
            "bundled": self.bundled,
            "runtime_only_actions": self.runtime_only_actions,
            "when_to_use": self.when_to_use,
        }


BUILTIN_CAPABILITY_DATA: list[dict[str, Any]] = [
    {
        "name": "calendar",
        "version": "0.1.0",
        "summary": "Dry-run calendar inspection actions for MVP workflows.",
        "actions": ["calendar.list_events", "calendar.get_event"],
        "permissions": {"calendar": "read"},
        "risk_level": "low",
        "handler": "dry_run.calendar",
    },
    {
        "name": "email",
        "version": "0.1.0",
        "summary": "Dry-run email search actions for MVP workflows.",
        "actions": ["email.search"],
        "permissions": {"email": "read"},
        "risk_level": "low",
        "handler": "dry_run.email",
    },
    {
        "name": "notifications",
        "version": "0.2.0",
        "summary": "Cross-platform desktop notifications (notify-send on Linux, osascript on macOS).",
        "actions": ["notifications.send"],
        "permissions": {"notifications": "send"},
        "risk_level": "low",
        "handler": "runtime.notifications",
    },
    {
        "name": "webchat",
        "version": "0.1.0",
        "summary": "Reply in the jiggaview browser chat (file-backed webchat channel).",
        "actions": ["webchat.send_message", "webchat.poll_messages"],
        "runtime_only_actions": ["webchat.poll_messages"],
        "risk_level": "low",
        "handler": "runtime.webchat",
    },
    {
        "name": "mailbox",
        "version": "0.1.0",
        "summary": "File-backed agent mailbox — send durable messages to a teammate's inbox (read on their next wake).",
        "actions": ["mailbox.send"],
        "permissions": {"mailbox": "send"},
        "risk_level": "low",
        "handler": "runtime.mailbox",
    },
    {
        "name": "tickets",
        "version": "0.1.0",
        "summary": "Move a team ticket across its board lanes, or list the team's tickets by lane (file-first, audited).",
        "actions": ["tickets.move", "tickets.list"],
        "permissions": {"tickets": "move"},
        "risk_level": "low",
        "handler": "runtime.tickets",
    },
    {
        "name": "summarization",
        "version": "0.1.0",
        "summary": "MVP text/context summarization actions.",
        "actions": ["summarize_day", "summarize_relevant_context"],
        "permissions": {"memory": "read"},
        "risk_level": "low",
        "handler": "dry_run.summarization",
    },

    {
        "name": "subagent-delegation",
        "version": "0.1.0",
        "summary": "Controlled subagent spawning through policy-checked runtime adapters.",
        "actions": ["spawn_subagent"],
        "permissions": {"delegation": "spawn_subagent"},
        "risk_level": "medium",
        "handler": "runtime.spawn_subagent",
    },
    {
        "name": "filesystem",
        "version": "0.1.0",
        "summary": "Native filesystem read/write/list/search actions, gated per-path by the executing agent's policy at runtime.",
        "actions": [
            "filesystem.read_file",
            "filesystem.write_file",
            "filesystem.list_directory",
            "filesystem.search_files",
        ],
        # Empty path lists mean: capability uses filesystem at runtime, the
        # actual paths come from each workflow step's input. The handler calls
        # evaluate_filesystem(agent, resolved_path, op) per-action; this is
        # what makes the bundle safe to ship at risk_level: low — the per-path
        # gating is the security boundary, not the declaration.
        "permissions": {"filesystem": {"read": [], "write": []}},
        # Declared shapes: without them every filesystem action was advertised
        # as taking an open object, so the model had to infer that read_file
        # wants `path` and search_files wants `pattern` from a shared one-line
        # summary — and probe when it guessed wrong.
        "action_inputs": {
            "filesystem.read_file": {
                "path": {"type": "string", "required": True,
                         "description": "Absolute or ~-relative path of the file to read."},
            },
            "filesystem.write_file": {
                "path": {"type": "string", "required": True,
                         "description": "Absolute or ~-relative path to write."},
                "content": {"type": "string", "required": True, "description": "Full new file content."},
                "overwrite": {"type": "boolean", "description": "Replace an existing file (default false)."},
                "create_parents": {"type": "boolean",
                                   "description": "Create missing parent directories (default false)."},
            },
            "filesystem.list_directory": {
                "path": {"type": "string", "required": True, "description": "Directory to list."},
                "recursive": {"type": "boolean", "description": "Walk subdirectories (default false)."},
                "glob": {"type": "string", "description": "Only entries matching this glob, e.g. '*.md'."},
            },
            "filesystem.search_files": {
                "path": {"type": "string", "required": True, "description": "Directory to search under."},
                "pattern": {"type": "string", "required": True,
                            "description": "Text or regex to find inside files."},
                "case_sensitive": {"type": "boolean", "description": "Default false."},
                "max_matches": {"type": "integer", "description": "Cap on returned matches."},
            },
        },
        "risk_level": "low",
        "handler": "runtime.filesystem",
    },
    {
        "name": "content-drafting",
        "version": "0.1.0",
        "summary": "Dry-run content strategy and drafting actions for demo workflows.",
        "actions": [
            "extract_core_message",
            "draft_linkedin_post",
            "draft_thread",
            "draft_blurb",
            "review_tone_and_claims",
            "prepare_distribution_package",
        ],
        # Paths are absolute so they match the content_strategist agent's
        # filesystem allow list (`~/Projects/content`, `~/Projects/content/drafts`)
        # without forcing every user to override either side. Capability paths
        # are evaluated against the executing agent's filesystem policy.
        "permissions": {
            "filesystem": {
                "read": ["~/Projects/content/**"],
                "write": ["~/Projects/content/drafts/**"],
            }
        },
        "risk_level": "low",
        "handler": "dry_run.generic",
    },
    {
        "name": "memory-write",
        "version": "0.1.0",
        "summary": "Persist a durable fact to the team's memory (team.jsonl) for later reuse/retrieval.",
        "actions": ["memory.remember"],
        "action_inputs": {
            "memory.remember": {
                "text": {"type": "string", "required": True,
                         "description": "The durable fact, decision or runbook to remember."},
                "type": {"type": "string",
                         "description": "fact | decision | preference | learning | runbook."},
                "tags": {"type": "array", "description": "Optional tags for retrieval."},
            },
        },
        "risk_level": "low",
        "handler": "runtime.remember",
    },
    {
        "name": "memory-search",
        "version": "0.1.0",
        "summary": "Search memory (raw entries + structured/summaries) by keyword, scope-aware. "
                   "Returns ranked snippets; backed by a sqlite FTS5 index that scales as memory grows.",
        "actions": ["memory.search"],
        "action_inputs": {
            "memory.search": {
                "query": {"type": "string", "required": True, "description": "Keywords to search for."},
                "limit": {"type": "integer", "description": "Maximum results (default 10)."},
            },
        },
        "risk_level": "low",
        "handler": "runtime.search_memory",
    },
    {
        "name": "web",
        "version": "0.1.0",
        "summary": "Read the open web: fetch a URL as extracted text (web.fetch) or run a "
                   "keyword web search (web.search). Fetch is double-gated: the host must be "
                   "in the config allowlist (web.allowed_domains) AND pass the executing "
                   "agent's network policy. Fetched content is untrusted input.",
        "when_to_use": "Research questions, reading documentation or articles, checking a "
                       "live page. Not for APIs needing auth — those belong in a dedicated capability.",
        "actions": ["web.fetch", "web.search"],
        # Untrusted remote content + network egress → approval-gated outside
        # autonomous mode. The real egress bound is the domain allowlist.
        "risk_level": "medium",
        "handler": "runtime.web",
    },
    {
        "name": "media",
        "version": "0.1.0",
        "summary": "Generate an image from a prompt (media.generate_image) via the "
                   "configured media.image provider. Returns the image inline; a step's "
                   "`output:` decides where it is written.",
        "when_to_use": "A workflow needs a picture — social post art, a diagram, a cover. "
                       "Needs an API-keyed provider configured under media.image; the text "
                       "model provider cannot serve this.",
        "actions": ["media.generate_image"],
        # Network egress to a third party plus real spend per call → approval
        # gated outside autonomous mode, like every other paid remote call.
        "risk_level": "medium",
        "permissions": {"network": {"mode": "allow"}},
        "handler": "runtime.media",
    },
    {
        "name": "shell",
        "version": "0.1.0",
        "summary": "Run a command as an argv list through the policy-gated safe-process "
                   "runner (never a shell: no pipes/redirection/expansion). The agent's own "
                   "shell policy (deny by default, restricted allowlist, dangerous-pattern "
                   "block) and a cwd execute check are enforced on every call; full "
                   "stdout/stderr are saved as run artifacts.",
        "when_to_use": "Running a local program or script the user asked for — builds, "
                       "git status, converters. Prefer a dedicated capability when one exists "
                       "(filesystem.*, web.*).",
        "actions": ["shell.run"],
        # Arbitrary process execution is the highest blast radius we ship —
        # approval-gated outside autonomous mode AND still subject to the
        # per-agent shell policy inside the runner (approval never overrides
        # a policy deny).
        "risk_level": "high",
        "handler": "runtime.shell",
    },
    {
        "name": "reminders",
        "version": "0.1.0",
        "summary": "One-shot reminders: remind.at schedules a single future wake (ISO time "
                   "or a relative offset like 30m/2h/1d); the supervisor fires it exactly "
                   "once as a task for the target agent. remind.list shows pending ones.",
        "when_to_use": "\"Remind me at 5pm\", \"ping me in 2 hours\", any single future "
                       "follow-up. For recurring schedules use wake.schedules (cron) instead.",
        "actions": ["remind.at", "remind.list"],
        "risk_level": "low",
        "handler": "runtime.reminders",
    },
    {
        "name": "text-generation",
        "version": "0.1.0",
        "summary": "Generate or transform text with the executing agent's configured model. "
                   "Unlike content-drafting (dry-run stubs), this makes a real model call, so a "
                   "workflow step can think — the step's input is the brief, prior step outputs "
                   "ride along as context.",
        "actions": ["draft_with_model"],
        # No special permissions: a model call isn't gated by a resource policy
        # (same as skill_pack capabilities and the agent loop). The agent must
        # simply have a model configured.
        "risk_level": "low",
        "handler": "runtime.draft_with_model",
    },
    {
        "name": "team-insight",
        "version": "0.1.0",
        "summary": "Read-only cross-team visibility for an orchestrator: list every team "
                   "(members + lead) and read any team's lead-curated plan/priorities, recent "
                   "status, and handoff decision log. File-first reads.",
        "actions": ["team.list", "team.status"],
        "risk_level": "low",
        "handler": "runtime.team_insight",
    },
    {
        "name": "team-orchestration",
        "version": "0.1.0",
        "summary": "Dispatch work across the org: run a team (team.run) or assign a task to any "
                   "agent (task.assign). Commands flow through the task queue + audit log, so they "
                   "stay file-first and auditable. For the default/chief agent.",
        "actions": ["team.run", "task.assign"],
        "risk_level": "medium",
        "handler": "runtime.team_orchestration",
    },
]


def bundled_capabilities() -> list[CapabilityManifest]:
    return [CapabilityManifest.from_dict(item, source="builtin", bundled=True) for item in BUILTIN_CAPABILITY_DATA]


def _manifest_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reject_symlinked_manifest(path: Path) -> None:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError(f"Capability manifest cannot be a symlink or live under a symlinked directory: {path}")


def load_capability_manifest(path: Path) -> CapabilityManifest:
    _reject_symlinked_manifest(path)
    return CapabilityManifest.from_dict(
        read_yaml(path),
        source=str(path),
        bundled=False,
        manifest_hash=_manifest_hash(path),
    )


@dataclass(frozen=True)
class CapabilityLoadError:
    """A manifest that is present on disk but could not be loaded.

    Assertion 15: a capability that fails to load must be a loud, enumerable
    state — not an absence. An empty capability list looks identical to a
    working system right up until something calls one.
    """

    name: str      # the directory name, which is what an operator recognizes
    path: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "reason": self.reason}


def scan_capability_dir(path: Path) -> tuple[list[CapabilityManifest], list[CapabilityLoadError]]:
    """Load every manifest under `path`, returning what loaded and what didn't.

    A broken manifest is recorded rather than raised: one unparseable file used
    to abort the whole registry load, taking every capability down with it and
    breaking commands that had nothing to do with it.
    """
    if not path.exists():
        return [], []
    manifests: list[CapabilityManifest] = []
    errors: list[CapabilityLoadError] = []
    for file in list_config_files(path):
        if file.name == "manifest.yaml" or file.name == "manifest.yml":
            try:
                manifests.append(load_capability_manifest(file))
            except Exception as exc:  # noqa: BLE001 — one bad manifest must not blind the rest
                errors.append(CapabilityLoadError(
                    name=file.parent.name, path=str(file),
                    reason=f"{type(exc).__name__}: {exc}".replace("\n", " ")[:200],
                ))
    return manifests, errors


def approvals_path(approvals_dir: Path) -> Path:
    return approvals_dir / APPROVALS_FILE


def load_approval_index(approvals_dir: Path) -> dict[str, dict[str, Any]]:
    """Read the recorded capability approvals as `{name: {manifest_hash, approved_at, ...}}`.

    Bundled capabilities are not subject to approval; this index covers only
    user-local packs. Returns an empty dict if no approvals file exists yet.
    """
    path = approvals_path(approvals_dir)
    if not path.exists():
        return {}
    raw = read_json(path)
    return dict(raw.get("approvals") or {})


def record_approval(approvals_dir: Path, capability: CapabilityManifest) -> dict[str, Any]:
    """Record an approval for a user-local capability pack.

    Keyed by capability name; the recorded manifest_hash is what gets compared
    on subsequent loads. Hash mismatch (manifest changed since approval) means
    the capability falls back to pending until re-approved.
    """
    if capability.bundled:
        raise ValueError("Bundled capabilities do not require approval")
    if capability.manifest_hash is None:
        raise ValueError("Cannot record approval for capability without a manifest_hash")
    ensure_dir(approvals_dir)
    path = approvals_path(approvals_dir)
    current = read_json(path) if path.exists() else {"version": 1, "approvals": {}}
    entry = {
        "manifest_hash": capability.manifest_hash,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "source": capability.source,
    }
    current.setdefault("approvals", {})[capability.name] = entry
    write_json(path, current)
    return entry


class CapabilityRegistry:
    def __init__(
        self,
        capabilities: list[CapabilityManifest],
        pending: list[CapabilityManifest] | None = None,
        load_errors: list[CapabilityLoadError] | None = None,
        pending_reasons: dict[str, str] | None = None,
    ):
        self.capabilities = capabilities
        self.pending = pending or []
        # Present on disk, unloadable. Enumerable so `doctor` can be loud about
        # it rather than reporting a smaller-but-healthy-looking registry.
        self.load_errors = load_errors or []
        # Why each pending capability is parked: "unapproved" (installed, never
        # approved) vs "changed" (approved once, the file differs since). Very
        # different severities — one is a routine next step, the other is drift.
        self.pending_reasons = pending_reasons or {}
        self._by_name = {capability.name: capability for capability in capabilities}
        self._by_action: dict[str, CapabilityManifest] = {}
        for capability in capabilities:
            for action in capability.actions:
                # Earlier capabilities win. Callers pass highest precedence first.
                self._by_action.setdefault(action, capability)

    @classmethod
    def load(
        cls,
        user_capabilities: Path | None = None,
        project_capabilities: Path | None = None,
        approvals_dir: Path | None = None,
    ) -> "CapabilityRegistry":
        """Load capability manifests.

        Precedence: project > user > bundled. When `approvals_dir` is given,
        user-local manifests must have a recorded approval whose manifest_hash
        matches the file on disk; unapproved or hash-mismatch entries land in
        `pending` and are not dispatched. Bundled capabilities are never gated
        by approval. Callers that don't pass `approvals_dir` keep the old
        behaviour (no gating) — this is the safe MVP default for tests and
        ad-hoc loads.
        """
        approved = load_approval_index(approvals_dir) if approvals_dir is not None else None
        active: list[CapabilityManifest] = []
        pending: list[CapabilityManifest] = []
        errors: list[CapabilityLoadError] = []
        reasons: dict[str, str] = {}
        for source in (project_capabilities, user_capabilities):
            if source is None:
                continue
            found, failed = scan_capability_dir(source)
            errors.extend(failed)
            for capability in found:
                _route_user_capability(capability, approved, active, pending, reasons)
        active.extend(bundled_capabilities())
        return cls(active, pending, load_errors=errors, pending_reasons=reasons)

    def list(self) -> list[CapabilityManifest]:
        return list(self.capabilities)

    def get(self, name: str) -> CapabilityManifest | None:
        return self._by_name.get(name)

    def resolve_action(self, action: str) -> CapabilityManifest | None:
        return self._by_action.get(action)

    def list_pending(self) -> list[CapabilityManifest]:
        return list(self.pending)

    def to_index(self) -> dict[str, Any]:
        return {
            "capabilities": [capability.to_dict() for capability in self.capabilities],
            "pending": [capability.to_dict() for capability in self.pending],
            "pending_reasons": dict(self.pending_reasons),
            "load_errors": [error.to_dict() for error in self.load_errors],
            "actions": {
                action: capability.name
                for action, capability in sorted(self._by_action.items())
            },
        }


def _route_user_capability(
    capability: CapabilityManifest,
    approved: dict[str, dict[str, Any]] | None,
    active: list[CapabilityManifest],
    pending: list[CapabilityManifest],
    reasons: dict[str, str] | None = None,
) -> None:
    if approved is None:
        active.append(capability)
        return
    recorded = approved.get(capability.name)
    if recorded is not None and recorded.get("manifest_hash") == capability.manifest_hash:
        active.append(capability)
        return
    pending.append(capability)
    if reasons is not None:
        # An approval that exists but no longer matches means the file changed
        # underneath it — drift or tampering, not a missing first approval.
        reasons[capability.name] = "changed" if recorded is not None else "unapproved"

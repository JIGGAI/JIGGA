"""Config references to providers and models outlive the names (assertion 14).

On the prior-gen stack an upgrade merged the standalone `openai-codex` provider
into `openai` with `agentRuntime: {id: "codex"}`. Every legacy
`openai-codex/*` reference was then rejected — `run error: Unknown model:
openai-codex/gpt-5.5` — and the official fix was a `doctor --fix` that rewrote
model refs across defaults, agents, *and stale sessions*. The sessions part is
the part people forget: config looks migrated, and old runs keep failing.

Two kinds of staleness are handled here, and only one of them needs a rename
table:

**Renamed** — a provider or model that has been deliberately renamed. Driven by
`MODEL_RENAMES`, which is intentionally empty today: JIGGA has not renamed
anything yet, and the point of the assertion is to have the migration path
*before* the first rename rather than after. Seeding it with an invented rename
would be worse than empty — it would be a lie the tests then enshrine.

**Dangling** — a reference to a profile or provider that simply isn't
configured. This needs no table and is live right now, because it fails
*silently*: `call_model` falls back to the default profile when a named one is
missing, so an agent you believe is pinned to a cheap model has been quietly
running on the default one. Nothing in the run record says so.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_agents
from jigga.core.io import read_json, read_yaml, write_json, write_yaml
from jigga.core.paths import JiggaPaths
from jigga.runtime.model_router import load_model_config

# old reference -> new reference. Applies to provider ids and to `provider/model`
# strings. Empty by design; see the module docstring.
MODEL_RENAMES: dict[str, str] = {}

# Keys whose *values* are model/provider references. The rewrite is targeted at
# these rather than done as a blind string replace, so a rename can never
# corrupt unrelated prose that happens to contain the old name.
_REFERENCE_KEYS = {"model", "provider", "model_profile", "primary", "default_model"}


def _renamed(value: Any) -> str | None:
    """The new name for `value`, or None if it isn't renamed."""
    if not isinstance(value, str):
        return None
    if value in MODEL_RENAMES:
        return MODEL_RENAMES[value]
    # `old-provider/model` — rename the provider half, keep the model.
    if "/" in value:
        provider, _, model = value.partition("/")
        if provider in MODEL_RENAMES:
            return f"{MODEL_RENAMES[provider]}/{model}"
    return None


def _rewrite(node: Any, found: list[tuple[str, str]]) -> Any:
    """Recursively rewrite renamed references under `_REFERENCE_KEYS`."""
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            if key in _REFERENCE_KEYS:
                new = _renamed(value)
                if new is not None:
                    found.append((str(value), new))
                    result[key] = new
                    continue
            result[key] = _rewrite(value, found)
        return result
    if isinstance(node, list):
        return [_rewrite(item, found) for item in node]
    return node


def stale_model_refs(paths: JiggaPaths) -> list[dict[str, Any]]:
    """Every model/provider reference that won't resolve as written.

    Returns rows of `{where, ref, problem, suggestion}` — `problem` is
    `renamed` (rewritable) or `dangling` (needs a human decision).
    """
    config = load_model_config(paths.home)
    providers = set((config.get("providers") or {}).keys())
    profiles = set((config.get("profiles") or {}).keys()) | {"default"}
    rows: list[dict[str, Any]] = []

    def _note(where: str, ref: str, problem: str, suggestion: str | None = None) -> None:
        rows.append({"where": where, "ref": ref, "problem": problem, "suggestion": suggestion})

    default_provider = (config.get("defaults") or {}).get("provider")
    if default_provider:
        new = _renamed(default_provider)
        if new:
            _note("config.yaml: models.defaults.provider", default_provider, "renamed", new)
        elif providers and default_provider not in providers:
            _note("config.yaml: models.defaults.provider", default_provider, "dangling")

    for profile_id, profile in (config.get("profiles") or {}).items():
        for slot in ("primary", *(profile.get("fallback") or [])):
            name = profile.get(slot) if slot == "primary" else slot
            if not name:
                continue
            new = _renamed(name)
            if new:
                _note(f"config.yaml: models.profiles.{profile_id}", name, "renamed", new)
            elif providers and name not in providers:
                _note(f"config.yaml: models.profiles.{profile_id}", name, "dangling")

    for agent_id, agent in sorted(load_agents(paths.agents).items()):
        raw = agent.model
        if not raw:
            continue
        new = _renamed(raw)
        if new:
            _note(f"agents/{agent_id}.yaml: model", raw, "renamed", new)
            continue
        if raw.startswith("profile:"):
            profile = raw.split(":", 1)[1] or "default"
            if profile not in profiles:
                # Silent today: call_model falls back to the default profile, so
                # the agent runs on a model nobody chose for it.
                _note(f"agents/{agent_id}.yaml: model", raw, "dangling")
    return rows


def _state_files(paths: JiggaPaths) -> list[Path]:
    """Session and run records — the state a config-only migration leaves behind."""
    files: list[Path] = []
    if paths.sessions.exists():
        files.extend(sorted(paths.sessions.glob("*.json")))
    if paths.runs.exists():
        files.extend(sorted(paths.runs.glob("**/run.json")))
    return files


def migrate_model_refs(paths: JiggaPaths, *, apply: bool = False) -> dict[str, Any]:
    """Rewrite renamed provider/model references across config, agents, and state.

    Returns `{"changed": [{path, from, to}], "applied": bool}`. With
    `apply=False` this is a dry run and nothing is written.
    """
    changed: list[dict[str, str]] = []

    def _do_yaml(path: Path) -> None:
        if not path.exists():
            return
        found: list[tuple[str, str]] = []
        data = _rewrite(read_yaml(path) or {}, found)
        if found:
            if apply:
                write_yaml(path, data)
            changed.extend({"path": str(path), "from": old, "to": new} for old, new in found)

    def _do_json(path: Path) -> None:
        try:
            raw = read_json(path)
        except (OSError, ValueError):
            return  # a corrupt record is the recovery sweep's problem, not ours
        found: list[tuple[str, str]] = []
        data = _rewrite(raw, found)
        if found:
            if apply:
                write_json(path, data)
            changed.extend({"path": str(path), "from": old, "to": new} for old, new in found)

    _do_yaml(paths.config)
    if paths.agents.exists():
        for agent_file in sorted(paths.agents.glob("*.yaml")):
            _do_yaml(agent_file)
    for state_file in _state_files(paths):
        _do_json(state_file)

    return {"changed": changed, "applied": apply}

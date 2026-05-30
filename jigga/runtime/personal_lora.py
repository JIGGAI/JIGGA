"""Personal LoRA capability handler — v0 scaffolding.

This module is the dispatch target for the `personal-lora` capability's four
actions (`lora.list`, `lora.train`, `lora.evaluate`, `lora.activate`). It
deliberately ships as a *sketch*: only `lora.list` does real work; the
others return structured `status: "planned"` payloads describing what the
v1 implementation will do. The shape of the returned dicts is the contract
the next agent should implement against.

See `docs/PERSONAL_LORA_RUNTIME_NOTES.md` for the full design — including
why each action exists, how it slots next to `memory_scope`, and the
sequenced plan for filling these stubs in.

## What lives here vs. elsewhere

This module is the *capability handler* — pure dispatch-time logic, called
from `dispatcher.py`. Three things explicitly do NOT belong here:

  1. **Model loading / inference.** A LoRA is *applied* in the model_router
     (new `kind: local_transformers` provider). This module only manages
     the adapter directory and the train/eval/activate lifecycle.
  2. **Training subprocess.** When the trainer lands, it spawns via
     `runtime.sandbox.run_sandboxed` (authority side) so it inherits the
     env allowlist + timeout discipline. The training wrapper code lives
     here; the actual `unsloth`/`axolotl` import lives only inside the
     subprocess.
  3. **CLI surface.** The actions are invoked through `dispatch_action`
     just like any capability. No bespoke `jigga lora ...` CLI is needed
     in v0 — workflow steps and `agent.run` already reach this code.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, read_json
from jigga.core.models import WorkflowStep
from jigga.runtime.audit import append_event
from jigga.runtime.capabilities import CapabilityManifest

# NB: RuntimeContext lives in `dispatcher.py` which imports this module's
# handler — so importing the type back would be a circular import. We follow
# the same pattern as `filesystem_handler` / `google_calendar_handler` /
# `telegram_handler` and take `runtime` un-typed. The dispatcher passes a
# `RuntimeContext` and these handlers read `runtime.home`, `runtime.logs_dir`,
# `runtime.agent` off it directly.


LORAS_DIRNAME = "loras"
ADAPTER_MANIFEST_FILENAME = "adapter.json"
EVAL_FILENAME = "eval.json"


# --- adapter discovery ------------------------------------------------------


@dataclass(frozen=True)
class AdapterRecord:
    """One adapter version on disk under ~/.jigga/loras/<name>/v<N>/.

    Mirrors what `adapter.json` will carry once the trainer lands. v0 reads
    whatever the manifest holds and treats missing fields as None — letting
    the user hand-create adapter directories during development without
    breaking `lora.list`.
    """

    name: str
    version: str
    path: str
    base_model: str | None = None
    training_run: str | None = None
    created_at: str | None = None
    eval_summary: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def loras_dir(home: Path) -> Path:
    return home / LORAS_DIRNAME


def discover_adapters(home: Path) -> list[AdapterRecord]:
    """Walk `~/.jigga/loras/<name>/v<N>/` and return a record per adapter.

    A directory layout that has not been set up yet (setup_fn never ran)
    returns an empty list rather than raising — the action is read-only and
    should be safe to call against any runtime.
    """
    root = loras_dir(home)
    if not root.exists():
        return []
    records: list[AdapterRecord] = []
    for name_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for version_dir in sorted(p for p in name_dir.iterdir() if p.is_dir()):
            records.append(_load_adapter_record(name_dir.name, version_dir))
    return records


def _load_adapter_record(name: str, version_dir: Path) -> AdapterRecord:
    manifest_path = version_dir / ADAPTER_MANIFEST_FILENAME
    base: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            base = read_json(manifest_path) or {}
        except (json.JSONDecodeError, OSError):
            # Tolerate a hand-edited or partial adapter dir — `lora.list`
            # should surface what's there, not refuse to list because one
            # manifest is malformed.
            base = {}
    eval_path = version_dir / EVAL_FILENAME
    eval_summary: dict[str, Any] | None = None
    if eval_path.exists():
        try:
            eval_summary = read_json(eval_path)
        except (json.JSONDecodeError, OSError):
            eval_summary = None
    return AdapterRecord(
        name=name,
        version=version_dir.name,
        path=str(version_dir),
        base_model=base.get("base_model"),
        training_run=base.get("training_run"),
        created_at=base.get("created_at"),
        eval_summary=eval_summary,
    )


# --- the four action handlers ----------------------------------------------


def _lora_list(
    runtime,
    _resolved_input: Any,
) -> dict[str, Any]:
    """Real implementation. Cheap, read-only, useful for setup verification."""
    adapters = discover_adapters(runtime.home)
    return {
        "source": "capability.personal_lora",
        "action": "lora.list",
        "status": "ok",
        "count": len(adapters),
        "adapters": [record.to_dict() for record in adapters],
    }


def _lora_train(
    runtime,
    resolved_input: Any,
) -> dict[str, Any]:
    """Stub. v1 will:
      1. Resolve training_scope -> corpus.
      2. Augment via model_router.
      3. Mix in general instruction data.
      4. Sandboxed trainer spawn via run_sandboxed.
      5. Auto-eval against previous version.
      6. Land adapter at ~/.jigga/loras/<name>/v<N>/.
      7. Emit lora.train.completed (does NOT activate).
    """
    payload = resolved_input if isinstance(resolved_input, dict) else {}
    return {
        "source": "capability.personal_lora",
        "action": "lora.train",
        "status": "planned",
        "implemented": False,
        "next_step": (
            "v1 training pipeline not yet implemented. See "
            "docs/PERSONAL_LORA_RUNTIME_NOTES.md section 'Training pipeline'."
        ),
        "would_have_used": {
            "training_scope": payload.get("training_scope"),
            "base_model": payload.get("base_model"),
            "hyperparams": payload.get("hyperparams") or {},
            "output_adapter": payload.get("output_adapter"),
        },
    }


def _lora_evaluate(
    runtime,
    resolved_input: Any,
) -> dict[str, Any]:
    """Stub. v1 will replay facts-back recall + a general-capability subset
    against the adapter and emit `lora.eval.passed` or `lora.eval.regressed`.
    """
    payload = resolved_input if isinstance(resolved_input, dict) else {}
    return {
        "source": "capability.personal_lora",
        "action": "lora.evaluate",
        "status": "planned",
        "implemented": False,
        "next_step": (
            "v1 eval harness not yet implemented. The catastrophic-forgetting "
            "guard is what makes activation safe — review before stubbing this "
            "into a no-op."
        ),
        "would_have_evaluated": {
            "adapter": payload.get("adapter"),
            "baseline": payload.get("baseline"),
            "eval_set": payload.get("eval_set"),
        },
    }


def _lora_activate(
    runtime,
    resolved_input: Any,
) -> dict[str, Any]:
    """Stub. v1 will swap a profile's `lora:` field to point at a specific
    adapter version, with a regression-threshold guard refusing activation
    when general-capability eval has dropped beyond a configurable limit.
    """
    payload = resolved_input if isinstance(resolved_input, dict) else {}
    return {
        "source": "capability.personal_lora",
        "action": "lora.activate",
        "status": "planned",
        "implemented": False,
        "next_step": (
            "v1 activation flow not yet implemented. Must mutate config.yaml "
            "through the existing plan/apply approval path; do not bypass."
        ),
        "would_have_activated": {
            "profile": payload.get("profile"),
            "adapter": payload.get("adapter"),
            "version": payload.get("version"),
        },
    }


# --- dispatcher entry point ------------------------------------------------


_ACTION_DISPATCH = {
    "lora.list": _lora_list,
    "lora.train": _lora_train,
    "lora.evaluate": _lora_evaluate,
    "lora.activate": _lora_activate,
}


def personal_lora_handler(
    step: WorkflowStep,
    capability: CapabilityManifest,
    resolved_input: Any,
    _memory_context: dict[str, Any],
    runtime,
) -> Any:
    """Single entry point dispatched from `dispatcher.HANDLERS`.

    All four actions share a handler signature; the per-action functions
    above keep the surface explicit. `dispatcher.dispatch_action` already
    emits `capability.invocation.started/completed`, so this module only
    emits action-specific events (`lora.train.planned`, etc.) when the v1
    implementation lands. v0 stubs return without extra audit traffic.
    """
    handler = _ACTION_DISPATCH.get(step.action)
    if handler is None:
        return {
            "source": "capability.personal_lora",
            "action": step.action,
            "status": "error",
            "error": f"Unknown personal_lora action: {step.action}",
        }
    ensure_dir(loras_dir(runtime.home))
    result = handler(runtime, resolved_input)
    # Stubs emit a single audit breadcrumb so a future `jigga trace` walk
    # surfaces that the action was invoked even though it did no real work.
    if result.get("status") == "planned":
        append_event(
            runtime.logs_dir,
            "lora.action.planned",
            status="ask",
            action=step.action,
            capability=capability.name,
            agent=runtime.agent.id if runtime.agent else None,
        )
    return result

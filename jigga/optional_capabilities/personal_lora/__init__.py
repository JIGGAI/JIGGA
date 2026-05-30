"""Personal LoRA first-party optional capability.

Sketch / v0 scaffolding. Provides `setup(paths, *, input_fn=input,
print_fn=print)` invoked by `jigga capabilities install personal-lora`
after the manifest is copied into the runtime.

The setup function creates `~/.jigga/loras/` and seeds an example
`training_scopes.yaml` so the user has a concrete shape to edit. It does
NOT verify GPU presence, download base models, or run any training — see
`docs/PERSONAL_LORA_RUNTIME_NOTES.md` for the full design and the
sequenced plan for the agent that picks this up.
"""

from __future__ import annotations

from typing import Callable

from jigga.core.io import ensure_dir, write_yaml


_EXAMPLE_TRAINING_SCOPES = {
    "training_scopes": {
        "voice": {
            "name": "Voice",
            "description": (
                "Source material for absorbing the user's writing voice and "
                "decision style. Pulls from authored notes and summaries; "
                "deliberately excludes raw private logs."
            ),
            "includes": [
                "memory/summaries",
                "memory/structured/preferences.yaml",
            ],
            "excludes": [
                "memory/raw/private",
                "secrets",
            ],
            "sensitivity": {
                "allow_sensitive": False,
                "require_approval_for_raw": True,
            },
        },
    },
}


_NEXT_STEPS = """
This is the v0 scaffold for personal LoRA support.

What just happened:
  - Created ~/.jigga/loras/ for adapter storage.
  - Seeded ~/.jigga/loras/training_scopes.yaml with an example 'voice' scope.
  - Manifest installed under ~/.jigga/capabilities/personal-lora/.

What does NOT work yet (by design — this is scaffolding):
  - `lora.train` returns a planned-run payload; no real training.
  - `lora.evaluate` is a stub.
  - `lora.activate` does not yet mutate config.yaml.
  - The model_router does not yet know how to apply a LoRA at inference.

Read docs/PERSONAL_LORA_RUNTIME_NOTES.md for the full design and the
sequenced next-PR plan.
"""


def setup(
    paths,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
) -> int:
    """Install scaffolding for personal LoRA support. Returns 0 on success.

    Idempotent: re-running does not overwrite an existing training_scopes
    file, so a user who has customised theirs is safe to re-install.
    """
    print_fn("\n=== Personal LoRA setup (v0 scaffolding) ===")

    loras_dir = paths.home / "loras"
    ensure_dir(loras_dir)
    print_fn(f"  Adapter directory ready at {loras_dir}")

    scopes_path = loras_dir / "training_scopes.yaml"
    if scopes_path.exists():
        print_fn(f"  Existing training scopes preserved at {scopes_path}")
    else:
        write_yaml(scopes_path, _EXAMPLE_TRAINING_SCOPES)
        print_fn(f"  Seeded example training scopes at {scopes_path}")

    print_fn(_NEXT_STEPS)
    return 0

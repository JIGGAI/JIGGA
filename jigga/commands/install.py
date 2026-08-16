"""Install / uninstall first-party optional capabilities.

`jigga capabilities install [name]` is the entry point. With no name, the
user is shown a numbered menu of available optional capabilities (the
`REGISTRY` in `jigga.optional_capabilities`). With a name, install runs
directly. After the manifest is copied into the runtime, the capability's
own `setup_fn` (if any) runs interactively — the Google Calendar one walks
the user through Google Cloud Console + the OAuth flow.

Uninstall removes the user-local manifest, drops the recorded approval, and
deletes secrets associated with the capability (for Google Calendar:
tokens + client config).

All interactive I/O is parameterised so tests can drive it deterministically
without mocks of stdin/stdout.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from jigga.core.io import ensure_dir, read_json, read_yaml, write_json
from jigga.core.paths import JiggaPaths
from jigga.runtime.audit import append_event
from jigga.runtime.term_select import Option, select_one, supports_picker
from jigga.optional_capabilities import (
    OptionalCapability,
    get_optional,
    list_available,
)
from jigga.runtime.capabilities import (
    approvals_path,
    load_capability_manifest,
    record_approval,
)


def _copy_instructions(manifest_path: Path, target_dir: Path, *,
                       print_fn: Callable[..., None] = print) -> None:
    """Copy a skill_pack's `instructions:` file alongside its installed manifest.

    The filename is taken from the manifest but reduced to its basename before
    use — a pack must not be able to name `../../something` and have the
    installer copy a file from outside its own directory.
    """
    try:
        declared = (read_yaml(manifest_path) or {}).get("instructions")
    except (OSError, ValueError):
        return
    if not declared:
        return
    name = Path(str(declared)).name
    source = manifest_path.parent / name
    if not source.is_file():
        print_fn(f"! {manifest_path.parent.name}: declares instructions {name!r} but the file is missing.")
        return
    shutil.copyfile(source, target_dir / name)


def install_capability(
    paths: JiggaPaths,
    name: str | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
) -> int:
    """Install a first-party optional capability. Returns 0 on success."""
    if name is None:
        choice = _prompt_for_capability(list_available(), input_fn=input_fn, print_fn=print_fn)
        if choice is None:
            return 0  # user cancelled cleanly
        name = choice.name

    optional = get_optional(name)
    if optional is None:
        print_fn(f"No optional capability named {name!r}.")
        print_fn("Available:")
        for cap in list_available():
            print_fn(f"  {cap.name}: {cap.summary}")
        return 1

    target_dir = paths.capabilities / optional.name
    target_manifest = target_dir / "manifest.yaml"
    already_installed = target_manifest.exists()
    ensure_dir(target_dir)
    shutil.copyfile(optional.manifest_path, target_manifest)
    # A skill_pack's instructions live in a sibling file the manifest names, and
    # `skills.read_instructions` resolves it relative to the installed manifest
    # — so copying only the manifest installs a skill that silently has nothing
    # to say. Confined to a bare filename: the manifest must not be able to
    # reach out of its own pack directory.
    _copy_instructions(optional.manifest_path, target_dir, print_fn=print_fn)
    if already_installed:
        print_fn(
            f"{optional.name!r} is already installed; re-running setup. "
            "Existing tokens/secrets are preserved unless setup overwrites them."
        )
    else:
        print_fn(f"Installed manifest at {target_manifest}.")

    if optional.setup_fn is not None:
        exit_code = optional.setup_fn(paths, input_fn=input_fn, print_fn=print_fn)
        if exit_code != 0:
            # Roll back the manifest copy only if this was a fresh install,
            # so a re-run setup that fails doesn't strip an already-working
            # capability.
            if not already_installed:
                target_manifest.unlink(missing_ok=True)
                try:
                    target_dir.rmdir()
                except OSError:
                    pass
                print_fn(
                    "Setup did not complete; rolled back manifest copy. "
                    "No approval recorded."
                )
            # A failed install is worth a record too: it distinguishes "never
            # attempted" from "attempted and rolled back", which is the
            # difference between a missing capability and a broken setup.
            append_event(paths.logs, "capability.install_failed", status="error",
                         capability=optional.name, exit_code=exit_code,
                         rolled_back=not already_installed)
            return exit_code

    capability = load_capability_manifest(target_manifest)
    record_approval(paths.policies, capability)
    # An install grants an agent new powers and auto-approves the manifest that
    # defines them — the most privileged mutation the CLI performs, and until
    # now the only one that left no trace. The hash is recorded because that is
    # what the approval is bound to: a later `changed` verdict from
    # `doctor --capabilities` is only meaningful against a known starting point.
    append_event(paths.logs, "capability.installed",
                 capability=capability.name, version=capability.version,
                 actions=capability.actions, risk_level=capability.risk_level,
                 manifest_hash=capability.manifest_hash, handler=capability.handler,
                 reinstall=already_installed, auto_approved=True)
    print_fn(f"\n{optional.name!r} installed and approved. You can now use:")
    for action in capability.actions:
        print_fn(f"  • {action}")
    return 0


def uninstall_capability(
    paths: JiggaPaths,
    name: str,
    *,
    print_fn: Callable[..., None] = print,
) -> int:
    """Remove a previously-installed optional capability. Removes the manifest,
    drops the recorded approval, and deletes per-capability secrets (today
    just google-calendar's client config + tokens). Returns 0 on success."""
    target_dir = paths.capabilities / name
    target_manifest = target_dir / "manifest.yaml"
    if not target_manifest.exists():
        print_fn(f"{name!r} is not installed.")
        return 1

    # Read the manifest before deleting it — afterwards there is nothing left
    # to say what was removed, which is precisely when the record matters.
    try:
        removed = load_capability_manifest(target_manifest)
        actions, version = removed.actions, removed.version
    except Exception:  # noqa: BLE001 — an unreadable manifest still uninstalls
        actions, version = [], None

    target_manifest.unlink()
    try:
        target_dir.rmdir()
    except OSError:
        pass

    _drop_approval(paths.policies, name)
    _drop_capability_secrets(paths.secrets, name, print_fn=print_fn)
    append_event(paths.logs, "capability.uninstalled", capability=name, version=version,
                 actions=actions, approval_dropped=True)
    print_fn(f"Uninstalled {name!r}.")
    return 0


def list_available_capabilities(*, print_fn: Callable[..., None] = print) -> int:
    available = list_available()
    if not available:
        print_fn("No optional capabilities available yet.")
        return 0
    print_fn("Available optional capabilities:")
    for cap in available:
        print_fn(f"  {cap.name:24s} {cap.summary}")
    return 0


def maybe_prompt_after_init(
    paths: JiggaPaths,
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
) -> int:
    """Called by `jigga init` to offer optional-capability install. Skipped
    silently when `interactive` is False (CI, --no-prompt, non-TTY stdin)."""
    if not interactive:
        return 0
    available = list_available()
    if not available:
        return 0
    print_fn("\nOptional capabilities are available (e.g. Google Calendar).")
    raw = input_fn("Install one now? [y/N]: ").strip().lower()
    if raw not in {"y", "yes"}:
        print_fn("Skipped. Run `jigga capabilities install` later when you're ready.")
        return 0
    return install_capability(paths, name=None, input_fn=input_fn, print_fn=print_fn)


# --- internals --------------------------------------------------------------


def _prompt_for_capability(
    available: list[OptionalCapability],
    *,
    input_fn: Callable[[str], str],
    print_fn: Callable[..., None],
) -> OptionalCapability | None:
    if not available:
        print_fn("No optional capabilities available yet.")
        return None
    if supports_picker():
        picked = select_one("Install an optional capability",
                            [Option(label=cap.name, detail=cap.summary) for cap in available])
        return available[picked] if picked is not None else None
    print_fn("\nAvailable optional capabilities:\n")
    for index, cap in enumerate(available, start=1):
        print_fn(f"  [{index}] {cap.name:24s} {cap.summary}")
    print_fn("")
    while True:
        raw = input_fn(f"Select [1-{len(available)}] or 'q' to cancel: ").strip().lower()
        if raw in {"q", "quit", "exit", ""}:
            return None
        try:
            choice = int(raw)
        except ValueError:
            print_fn("  Not a number. Try again.")
            continue
        if 1 <= choice <= len(available):
            return available[choice - 1]
        print_fn(f"  Out of range. Pick 1-{len(available)} or 'q'.")


def _drop_approval(policies_dir: Path, name: str) -> None:
    path = approvals_path(policies_dir)
    if not path.exists():
        return
    payload = read_json(path)
    approvals = payload.get("approvals") or {}
    if name in approvals:
        del approvals[name]
        payload["approvals"] = approvals
        write_json(path, payload)


def _drop_capability_secrets(
    secrets_dir: Path,
    name: str,
    *,
    print_fn: Callable[..., None],
) -> None:
    """Remove per-capability secret files at uninstall time. Currently
    google-calendar is the only optional capability with secrets — when more
    land, extend this with a small dispatch dict."""
    if name == "google-calendar":
        for filename in ("google_calendar_client.json", "google_calendar_tokens.json"):
            candidate = secrets_dir / filename
            if candidate.exists():
                candidate.unlink()
                print_fn(f"Removed {candidate}.")

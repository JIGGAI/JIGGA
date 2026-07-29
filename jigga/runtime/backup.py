"""Backup & restore — roadmap "production needs" item 4.

`~/.jigga/` *is* the product state (file-first by design), so backup is an
archive of the home with judgment about what belongs in it:

- **Excluded by default:** `secrets/` (credentials never leave the machine
  unless `--include-secrets` is explicit), `runs/` and `sessions/` (ephemeral
  artifacts), `logs/archive/` (rotated history), `memory/indexes/` (rebuilt on
  demand), and `plugins/` (reinstallable, node_modules-sized).
- **Included:** agents, teams, workflows, memory, workspaces, tasks, policies,
  reminders, email drafts, config.yaml, USER.md, state.json, the live audit
  log — everything that makes the install *yours*.

Encryption stays honest about the dependency policy: the core ships no crypto,
so `--encrypt` pipes the archive through `age` or `gpg` **if installed**
(age: recipient or passphrase; gpg: symmetric passphrase) and refuses loudly
when neither is present, rather than shipping homegrown crypto.

Restore refuses to touch a non-empty home unless `--force`, and `--force`
moves the existing home aside (`<home>.pre-restore-<ts>`) before extracting —
a restore must never be the thing that destroys the only copy.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jigga.core.io import write_json
from jigga.core.models import now_iso

DEFAULT_EXCLUDES = ("secrets", "runs", "sessions", "logs/archive", "memory/indexes", "plugins")
_MANIFEST = "jigga-backup.json"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _excluded(rel: str, excludes: tuple[str, ...]) -> bool:
    return any(rel == e or rel.startswith(e + "/") for e in excludes)


def create_backup(
    home: Path,
    output: Path | None = None,
    *,
    include_secrets: bool = False,
    encrypt: str | None = None,   # None | "age" | "gpg"
    recipient: str | None = None,  # age -r recipient (else passphrase mode)
) -> dict[str, Any]:
    home = Path(home).resolve()
    if not home.is_dir():
        raise ValueError(f"Runtime home not found: {home}")
    excludes = tuple(e for e in DEFAULT_EXCLUDES if not (include_secrets and e == "secrets"))
    output = Path(output) if output else Path.cwd() / f"jigga-backup-{_stamp()}.tar.gz"
    if output.exists():
        raise ValueError(f"Refusing to overwrite existing file: {output}")

    included: list[str] = []
    with tarfile.open(output, "w:gz") as tar:
        for path in sorted(home.rglob("*")):
            rel = path.relative_to(home).as_posix()
            if _excluded(rel, excludes) or path.is_symlink():
                continue
            if path.is_file():
                tar.add(path, arcname=rel)
                included.append(rel)
        manifest = {
            "created_at": now_iso(), "home": str(home), "files": len(included),
            "include_secrets": include_secrets, "excluded": list(excludes),
        }
        manifest_path = output.parent / f".{output.name}.manifest.tmp"
        write_json(manifest_path, manifest)
        tar.add(manifest_path, arcname=_MANIFEST)
        manifest_path.unlink()

    result: dict[str, Any] = {"archive": str(output), "files": len(included),
                              "include_secrets": include_secrets, "encrypted": None}
    if encrypt:
        result = _encrypt(output, encrypt, recipient, result)
    return result


def _encrypt(archive: Path, tool: str, recipient: str | None, result: dict[str, Any]) -> dict[str, Any]:
    if tool not in ("age", "gpg"):
        raise ValueError(f"Unknown encryption tool: {tool!r} (age or gpg)")
    if shutil.which(tool) is None:
        archive.unlink()  # don't leave the plaintext archive behind on a failed contract
        raise ValueError(
            f"--encrypt {tool} requires the `{tool}` binary on PATH (JIGGA ships no crypto "
            f"of its own). Install it, or copy the archive somewhere already encrypted.")
    encrypted = archive.with_suffix(archive.suffix + (".age" if tool == "age" else ".gpg"))
    if tool == "age":
        cmd = ["age", "-o", str(encrypted)] + (["-r", recipient] if recipient else ["-p"]) + [str(archive)]
    else:
        cmd = ["gpg", "--symmetric", "--cipher-algo", "AES256", "-o", str(encrypted), str(archive)]
    completed = subprocess.run(cmd, check=False)  # interactive: passphrase prompts go to the tty
    if completed.returncode != 0 or not encrypted.exists():
        raise RuntimeError(f"{tool} exited {completed.returncode}; plaintext archive kept at {archive}")
    archive.unlink()
    result.update({"archive": str(encrypted), "encrypted": tool})
    return result


def inspect_backup(archive: Path) -> dict[str, Any]:
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.getmember(_MANIFEST)
        extracted = tar.extractfile(member)
        if extracted is None:
            raise ValueError("Backup manifest unreadable")
        import json

        return json.loads(extracted.read().decode("utf-8"))


def restore_backup(archive: Path, home: Path, *, force: bool = False) -> dict[str, Any]:
    archive = Path(archive)
    if archive.suffix in (".age", ".gpg"):
        raise ValueError(f"Decrypt first (this archive is {archive.suffix[1:]}-encrypted): "
                         f"`{archive.suffix[1:]} -d {archive.name}` → .tar.gz, then restore that.")
    home = Path(home).resolve()
    manifest = inspect_backup(archive)  # validates it IS a jigga backup before touching anything
    moved_aside = None
    if home.exists() and any(home.iterdir()):
        if not force:
            raise ValueError(f"Restore target {home} is not empty — pass --force to move it aside first.")
        moved_aside = home.with_name(home.name + f".pre-restore-{_stamp()}")
        home.rename(moved_aside)
    home.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        # `data` filter (Py3.12+): refuses absolute paths / .. traversal / links
        # escaping the target — a hostile archive can't write outside `home`.
        tar.extractall(home, filter="data")
    (home / _MANIFEST).unlink(missing_ok=True)
    return {"home": str(home), "files": manifest.get("files"),
            "created_at": manifest.get("created_at"),
            "moved_aside": str(moved_aside) if moved_aside else None}

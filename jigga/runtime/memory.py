from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_memory_scopes
from jigga.core.io import ensure_dir, write_json
from jigga.core.models import MemoryScope, now_iso
from jigga.runtime.audit import append_event, new_id


def inspect_memory(memory_dir: Path) -> dict[str, Any]:
    scopes = load_memory_scopes(memory_dir)
    return {
        "root": str(memory_dir),
        "layers": {
            "raw": str(memory_dir / "raw"),
            "structured": str(memory_dir / "structured"),
            "summaries": str(memory_dir / "summaries"),
            "indexes": str(memory_dir / "indexes"),
        },
        "scopes": {key: scope.to_dict() for key, scope in scopes.items()},
    }


def build_context_package(memory_dir: Path, scope_id: str) -> dict[str, Any]:
    scopes = load_memory_scopes(memory_dir)
    scope = scopes.get(scope_id) or MemoryScope(id=scope_id, description="Unknown scope; no memory included.")
    included: list[dict[str, Any]] = []
    for include in scope.includes:
        rel = include.removeprefix("memory/")
        target = memory_dir / rel
        if target.exists() and target.is_file():
            included.append({"path": str(target), "content": target.read_text(encoding="utf-8")})
        else:
            included.append({"path": str(target), "missing": True})
    return {"scope": scope.to_dict(), "included": included, "excluded": scope.excludes}


def write_memory_result(memory_dir: Path, logs_dir: Path, kind: str, content: Any, source: dict[str, Any]) -> Path:
    ensure_dir(memory_dir / "raw")
    entry = {
        "id": new_id("memory"),
        "time": now_iso(),
        "type": kind,
        "content": content,
        "source": source,
    }
    target = memory_dir / "raw" / f"{entry['id']}.json"
    write_json(target, entry)
    append_event(logs_dir, "memory.write", memory_type=kind, path=str(target), source=source)
    return target

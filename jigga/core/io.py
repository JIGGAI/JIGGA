from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import yaml


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write_text(path: Path, content: str) -> None:
    # Write to a temp sibling, then os.replace — atomic on POSIX, avoids
    # partial-file races between concurrent readers and writers.
    ensure_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path: Path, value: Any) -> None:
    _atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def write_yaml(path: Path, value: Any) -> None:
    _atomic_write_text(path, yaml.safe_dump(value, sort_keys=False))


def copy_if_missing(source: Path, target: Path) -> bool:
    if target.exists():
        return False
    ensure_dir(target.parent)
    shutil.copy2(source, target)
    return True


def list_config_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(
        item
        for item in path.rglob("*")
        if item.is_file() and item.suffix.lower() in {".yaml", ".yml", ".json"}
    )

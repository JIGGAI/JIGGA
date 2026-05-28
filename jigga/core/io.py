from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import yaml


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def write_yaml(path: Path, value: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


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

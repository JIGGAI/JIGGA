from __future__ import annotations

from pathlib import Path

from jigga.core.io import read_json, write_json
from jigga.core.models import State


def read_state(path: Path) -> State:
    if not path.exists():
        return State()
    return State.from_dict(read_json(path))


def write_state(path: Path, state: State) -> None:
    write_json(path, state.to_dict())

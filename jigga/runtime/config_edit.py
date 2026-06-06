"""Dotted-path get/set over the runtime config (`~/.jigga/config.yaml`).

The CLI surface the UI's settings page drives (`jigga config get|set|unset`
— CLI-as-API), and the human-friendly way to flip a key without opening an
editor (`jigga config set channels.default telegram`).

Values are coerced JSON-first: `true`/`42`/`1.5`/`[1,2]`/`{"a":1}` parse to
their types; anything that isn't valid JSON stays a plain string (so
`jigga config set channels.default telegram` needs no quoting).
"""

from __future__ import annotations

import json
from typing import Any

_MISSING = object()


def coerce_value(raw: str) -> Any:
    """JSON-first coercion with plain-string fallback."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def get_path(config: dict[str, Any], dotted: str) -> Any:
    """The value at a dotted path, or None when absent (absent and null are
    indistinguishable here — fine for a read surface)."""
    node: Any = config
    for part in str(dotted).split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def set_path(config: dict[str, Any], dotted: str, value: Any) -> Any:
    """Set a dotted path (creating intermediate maps), returning the old value
    (None when it didn't exist). Refuses to descend through a non-map — that
    would silently destroy a scalar the user didn't name."""
    parts = str(dotted).split(".")
    if not all(parts):
        raise ValueError(f"Invalid config key: {dotted!r}")
    node = config
    for part in parts[:-1]:
        existing = node.get(part, _MISSING)
        if existing is _MISSING or existing is None:
            node[part] = {}
        elif not isinstance(existing, dict):
            raise ValueError(
                f"Config key {dotted!r} descends through {part!r}, which holds a "
                f"{type(existing).__name__}, not a map — unset it first if you mean to replace it.")
        node = node[part]
    old = node.get(parts[-1])
    node[parts[-1]] = value
    return old


def unset_path(config: dict[str, Any], dotted: str) -> Any:
    """Remove a dotted path; returns the removed value (None when absent).
    Empty intermediate maps are left in place (harmless, and predictable)."""
    parts = str(dotted).split(".")
    node: Any = config
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if not isinstance(node, dict):
        return None
    return node.pop(parts[-1], None)

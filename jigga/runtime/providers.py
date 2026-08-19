"""Capabilities that provide an implementation, not just an action.

A capability pack has always been able to add TOOLS: an action name, a handler,
and the agent calls it. That is the wrong shape for something the runtime needs
to consult on its own — a memory backend has to be written to when memory lands
and read from when memory is searched, and neither is a tool call.

So a manifest can also declare what it PROVIDES:

    provides:
      memory.vector: memory_vector_local.backend:LocalVectorIndex

The runtime resolves a configured backend name through this registry exactly as
`dispatcher.resolve_handler` resolves an action — same `module:attr` import, same
trust boundary. What this buys is dependency isolation: Graphiti pulls in a graph
database and an embedding model, and JIGGA's core is stdlib + PyYAML. A pack owns
its dependencies and installs them itself, so an install that never enables graph
memory never pays for it.

Trust: the import target comes from a manifest the user installed and approved.
This module does not sandbox it — the capability approval flow is the gate, the
same one that already governs `handler:` imports.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

# Known slots. A manifest may declare any string, but an unknown slot is a
# typo nine times out of ten, and a silently ignored backend is the kind of
# thing you discover in production — so `jigga doctor` warns about names that
# are not in here.
KNOWN_SLOTS = (
    "memory.keyword",
    "memory.vector",
    "memory.graph",
)


@lru_cache(maxsize=64)
def resolve_provider(path: str) -> Any:
    """Import a `module.path:attr` provider reference and return the attribute.

    Returns the attribute itself — usually a class the caller instantiates,
    sometimes a factory. Unlike a handler it is not required to be callable:
    a provider is an object with methods, and demanding callability here would
    rule out the obvious implementations.
    """
    if ":" not in path:
        raise ValueError(
            f"Provider {path!r} must be a 'module.path:attr' import reference.")
    module_name, _, attr = path.partition(":")
    if not module_name or not attr:
        raise ValueError(f"Invalid provider reference: {path!r}")
    import importlib

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        # The pack is installed but its dependencies are not — the single most
        # likely failure, and one whose real message ("No module named 'kuzu'")
        # is far more useful than "provider unavailable".
        raise ValueError(f"Cannot import provider module {module_name!r}: {exc}") from exc
    provider = getattr(module, attr, None)
    if provider is None:
        raise ValueError(f"Provider {path!r} not found in {module_name!r}")
    return provider


def providers_for(registry: Any, slot: str) -> dict[str, str]:
    """{capability name: import path} for every installed capability that fills
    `slot`. Ordered as the registry lists them, which is deterministic."""
    found: dict[str, str] = {}
    # `.capabilities` only — a pack that is installed but not yet approved sits
    # in `.pending`, and an unapproved pack must not get its code imported into
    # the runtime just because a config line names it.
    for manifest in getattr(registry, "capabilities", None) or []:
        path = (getattr(manifest, "provides", None) or {}).get(slot)
        if path:
            found[manifest.name] = str(path)
    return found


def resolve_backend(registry: Any, slot: str, name: str) -> tuple[Any, str | None]:
    """The provider a config selected, or (None, reason).

    A reason rather than an exception: a memory search whose optional vector
    backend failed to load should degrade to keyword results and say so, not
    fail. The caller decides how loud to be — the audit log, usually.
    """
    if not name or name in {"none", "off"}:
        return None, None
    available = providers_for(registry, slot)
    path = available.get(name)
    if path is None:
        return None, (f"no installed capability provides {slot}={name!r}"
                      + (f" (available: {', '.join(sorted(available))})" if available else ""))
    try:
        return resolve_provider(path), None
    except ValueError as exc:
        return None, str(exc)

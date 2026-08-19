"""Memory search across whatever backends are installed.

`memory_index.search_memory()` is the keyword backend: SQLite FTS5 over the
memory tree, and the only one JIGGA ships. `docs/MEMORY_BACKENDS.md` proposes
vector and graph backends alongside it, supplied by capability packs so their
dependencies stay out of core (see `runtime/providers.py`).

This is the router that makes that possible. With nothing configured it calls
the keyword backend and returns exactly what it returned before — same shape,
same order, same results. With a vector or graph backend installed and selected,
it queries them too and fuses the rankings.

Fusion is reciprocal rank fusion: each backend contributes 1/(k + rank) per hit,
summed per document. RRF is the default `MEMORY_BACKENDS.md` specifies, and it
needs no score calibration between backends — comparing a BM25 score to a cosine
similarity directly would be meaningless, and normalising them would invent a
precision neither has.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jigga.core.config import load_runtime_config
from jigga.runtime.memory_index import search_memory
from jigga.runtime.providers import resolve_backend

# The constant from the original RRF paper. Large enough that the top few ranks
# are close together, so one backend being confident does not steamroll the rest.
RRF_K = 60


def configured_backends(home: Path) -> dict[str, str]:
    """`memory.backends` from config, with today's behaviour as the default."""
    config = (load_runtime_config(Path(home)).get("memory") or {}).get("backends") or {}
    return {
        "keyword": str(config.get("keyword", "file")),
        "vector": str(config.get("vector", "none")),
        "graph": str(config.get("graph", "none")),
    }


def _registry_for(home: Path) -> Any:
    """Load an approval-gated registry, lazily.

    Only called when a non-keyword backend is actually configured, so the
    default install pays nothing: today's search does not scan the capability
    directory, and it should not start.
    """
    from jigga.core.paths import get_paths
    from jigga.runtime.capabilities import CapabilityRegistry

    paths = get_paths(home)
    return CapabilityRegistry.load(user_capabilities=paths.capabilities,
                                   approvals_dir=paths.policies)


def _hit_key(hit: dict[str, Any]) -> str:
    """What makes two hits the same document across backends.

    Path, because that is the one identifier every backend can produce: the
    keyword index rows are files, a vector store's metadata carries the source
    path, and a graph fact points back at the `raw/` episode it came from.
    """
    return str(hit.get("path") or hit.get("id") or hit.get("source") or "")


def _fuse(ranked: list[tuple[str, list[dict[str, Any]]]], limit: int) -> list[dict[str, Any]]:
    fused: dict[str, dict[str, Any]] = {}
    scores: dict[str, float] = {}
    for backend, hits in ranked:
        for rank, hit in enumerate(hits, start=1):
            key = _hit_key(hit)
            if not key:
                continue
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
            if key not in fused:
                fused[key] = dict(hit)
                fused[key]["backends"] = []
            # Which backends found it is worth keeping: a hit every backend
            # agrees on is a different kind of result from one only the vector
            # store liked, and the caller can see that without re-querying.
            fused[key]["backends"].append(backend)
    ordered = sorted(fused.values(), key=lambda h: -scores[_hit_key(h)])
    for hit in ordered:
        # Unrounded: this is a comparable number a caller may sort or threshold
        # on, and 6dp was a display choice imposed on data.
        hit["fusion_score"] = scores[_hit_key(hit)]
    return ordered[:limit]


def search(
    home: Path,
    query: str,
    *,
    scope: str | None = None,
    team: str | None = None,
    limit: int = 10,
    rebuild: bool = False,
    registry: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Ranked memory hits, plus any degradations worth telling the caller about.

    The second element is the honest part. An optional backend that failed to
    load must not fail the search — but it must not vanish either, or a person
    reads keyword-only results believing their graph answered.
    """
    home = Path(home)
    backends = configured_backends(home)
    notes: list[str] = []
    ranked: list[tuple[str, list[dict[str, Any]]]] = []

    if backends["keyword"] != "none":
        ranked.append(("keyword", search_memory(home / "memory", query, scope=scope,
                                                team=team, limit=limit, rebuild=rebuild)))

    for slot in ("vector", "graph"):
        name = backends[slot]
        if name in {"none", "off", ""}:
            continue
        if registry is None:
            registry = _registry_for(home)
        provider, reason = resolve_backend(registry, f"memory.{slot}", name)
        if provider is None:
            notes.append(f"{slot} backend unavailable — {reason}")
            continue
        try:
            index = provider(home) if isinstance(provider, type) else provider
            hits = index.search(query, k=limit, scope=scope, team=team)
        except Exception as exc:  # noqa: BLE001 — a third-party backend may raise anything
            notes.append(f"{slot} backend {name!r} failed: {exc}")
            continue
        ranked.append((slot, list(hits or [])))

    if len(ranked) == 1:
        # One backend: return its own ranking untouched. Fusing a single list
        # would only reorder ties and add a score nobody asked for.
        return ranked[0][1][:limit], notes
    return _fuse(ranked, limit), notes

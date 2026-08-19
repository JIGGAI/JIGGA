"""A capability pack can supply a memory backend without core importing it.

`docs/MEMORY_BACKENDS.md` wants vector and graph memory. Both mean heavy
dependencies — an embedding model, a graph database — and JIGGA's core is
stdlib + PyYAML. So the implementation belongs in a pack that owns its own
dependencies, and core needs one seam to reach it.

Two rules these tests defend:
  1. An install with nothing configured behaves EXACTLY as before.
  2. An optional backend that is missing, broken, or unapproved degrades the
     search — it never fails it, and never disappears quietly.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from jigga.commands.init import init_runtime
from jigga.core.io import write_yaml
from jigga.runtime.capabilities import CapabilityRegistry
from jigga.runtime.memory_router import RRF_K, configured_backends, search
from jigga.runtime.providers import providers_for, resolve_backend, resolve_provider


class _FakeIndex:
    """What a pack would ship: constructed with the home, answers `search`."""

    def __init__(self, home: Path) -> None:
        self.home = home

    def search(self, query: str, *, k: int = 10, scope=None, team=None):
        return [{"path": "vec/only.md", "snippet": query, "score": 0.1}]


def _install_fake_module(monkeypatch, name: str = "fake_vector_pack", obj=_FakeIndex) -> None:
    module = types.ModuleType(name)
    module.Index = obj
    monkeypatch.setitem(sys.modules, name, module)
    resolve_provider.cache_clear()


def _pack(paths, *, name: str, provides: dict[str, str]) -> None:
    """A user-local capability that provides a backend, pre-approved."""
    from jigga.runtime.capabilities import load_capability_manifest, record_approval

    pack_dir = paths.capabilities / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(pack_dir / "manifest.yaml", {
        "name": name, "version": "0.1.0", "summary": "test backend",
        "type": "native", "actions": [], "provides": provides,
    })
    manifest = load_capability_manifest(pack_dir / "manifest.yaml")
    record_approval(paths.policies, manifest)


def _registry(paths) -> CapabilityRegistry:
    return CapabilityRegistry.load(user_capabilities=paths.capabilities,
                                   approvals_dir=paths.policies)


def _memory(paths, name: str = "note.md", text: str = "the launch plan is Friday") -> None:
    target = paths.memory / "structured" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


# --- resolving a provider ----------------------------------------------------


def test_it_imports_the_declared_attribute(monkeypatch) -> None:
    _install_fake_module(monkeypatch)
    assert resolve_provider("fake_vector_pack:Index") is _FakeIndex


def test_a_missing_dependency_says_which_one(monkeypatch) -> None:
    # The likeliest failure by far: the pack is installed, its dependency is
    # not. "No module named 'kuzu'" is worth more than "provider unavailable".
    resolve_provider.cache_clear()
    with pytest.raises(ValueError, match="kuzu_pack"):
        resolve_provider("kuzu_pack.backend:Index")


@pytest.mark.parametrize("ref", ["no_colon_here", ":Index", "module:"])
def test_a_malformed_reference_is_refused(ref: str) -> None:
    resolve_provider.cache_clear()
    with pytest.raises(ValueError):
        resolve_provider(ref)


def test_an_unapproved_pack_does_not_get_imported(tmp_path: Path, monkeypatch) -> None:
    # An unapproved capability sits in `pending`. Naming it in config must not
    # be enough to get its code loaded — approval is the gate, not the config.
    paths = init_runtime(tmp_path)
    _install_fake_module(monkeypatch)
    pack_dir = paths.capabilities / "rogue"
    pack_dir.mkdir(parents=True, exist_ok=True)
    write_yaml(pack_dir / "manifest.yaml", {
        "name": "rogue", "version": "0.1.0", "summary": "not approved",
        "type": "native", "actions": [], "provides": {"memory.vector": "fake_vector_pack:Index"},
    })
    registry = _registry(paths)

    assert providers_for(registry, "memory.vector") == {}
    provider, reason = resolve_backend(registry, "memory.vector", "rogue")
    assert provider is None and "no installed capability provides" in reason


def test_an_approved_pack_is_found_by_slot(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    _install_fake_module(monkeypatch)
    _pack(paths, name="vec", provides={"memory.vector": "fake_vector_pack:Index"})

    registry = _registry(paths)

    assert providers_for(registry, "memory.vector") == {"vec": "fake_vector_pack:Index"}
    provider, reason = resolve_backend(registry, "memory.vector", "vec")
    assert provider is _FakeIndex and reason is None


def test_none_selects_nothing_without_touching_the_registry() -> None:
    provider, reason = resolve_backend(object(), "memory.vector", "none")
    assert provider is None and reason is None


# --- the default install is unchanged ---------------------------------------


def test_defaults_are_keyword_only(tmp_path: Path) -> None:
    init_runtime(tmp_path)
    assert configured_backends(tmp_path) == {"keyword": "file", "vector": "none", "graph": "none"}


def test_search_with_no_backends_returns_the_keyword_results_verbatim(tmp_path: Path) -> None:
    # Not "equivalent" — identical. A router that reorders or re-scores a
    # single backend's results has changed behaviour for every existing install.
    from jigga.runtime.memory_index import search_memory

    paths = init_runtime(tmp_path)
    _memory(paths)

    direct = search_memory(paths.memory, "launch", limit=10)
    routed, notes = search(tmp_path, "launch", limit=10)

    assert routed == direct
    assert notes == []


# --- an optional backend joins in -------------------------------------------


def _configure(home: Path, **backends: str) -> None:
    from jigga.core.io import read_yaml, write_yaml as write

    config = read_yaml(home / "config.yaml") or {}
    config.setdefault("memory", {})["backends"] = backends
    write(home / "config.yaml", config)


def test_a_vector_backends_hits_are_fused_in(tmp_path: Path, monkeypatch) -> None:
    paths = init_runtime(tmp_path)
    _memory(paths)
    _install_fake_module(monkeypatch)
    _pack(paths, name="vec", provides={"memory.vector": "fake_vector_pack:Index"})
    _configure(tmp_path, keyword="file", vector="vec")

    results, notes = search(tmp_path, "launch", limit=10, registry=_registry(paths))

    assert notes == []
    paths_found = [r.get("path") for r in results]
    assert "vec/only.md" in paths_found, "the vector backend's hit must survive fusion"
    assert any("note.md" in str(p) for p in paths_found), "and so must the keyword hit"


def test_a_hit_both_backends_agree_on_ranks_first(tmp_path: Path, monkeypatch) -> None:
    # The point of fusion: agreement is signal. Two backends each ranking a
    # document first should beat a document only one of them found.
    paths = init_runtime(tmp_path)
    _memory(paths, name="shared.md", text="launch launch launch")

    class Agreeing(_FakeIndex):
        def search(self, query, *, k=10, scope=None, team=None):
            return [{"path": str(self.home / "memory" / "structured" / "shared.md"),
                     "snippet": "same doc"},
                    {"path": "vec/lonely.md", "snippet": "only here"}]

    _install_fake_module(monkeypatch, obj=Agreeing)
    _pack(paths, name="vec", provides={"memory.vector": "fake_vector_pack:Index"})
    _configure(tmp_path, keyword="file", vector="vec")

    results, _notes = search(tmp_path, "launch", limit=10, registry=_registry(paths))

    assert "shared.md" in str(results[0]["path"])
    assert sorted(results[0]["backends"]) == ["keyword", "vector"]
    assert results[0]["fusion_score"] == pytest.approx(2 / (RRF_K + 1))


# --- degradation is visible, never fatal ------------------------------------


def test_a_configured_but_missing_backend_degrades_with_a_reason(tmp_path: Path) -> None:
    paths = init_runtime(tmp_path)
    _memory(paths)
    _configure(tmp_path, keyword="file", vector="not_installed")

    results, notes = search(tmp_path, "launch", limit=10, registry=_registry(paths))

    assert results, "keyword results must still come back"
    assert any("not_installed" in note for note in notes)


def test_a_backend_that_raises_does_not_fail_the_search(tmp_path: Path, monkeypatch) -> None:
    # A third-party backend can raise anything at all. Memory search is on the
    # agent's hot path; a broken optional index must cost recall, not the run.
    paths = init_runtime(tmp_path)
    _memory(paths)

    class Exploding(_FakeIndex):
        def search(self, query, *, k=10, scope=None, team=None):
            raise RuntimeError("kuzu segfaulted")

    _install_fake_module(monkeypatch, obj=Exploding)
    _pack(paths, name="vec", provides={"memory.vector": "fake_vector_pack:Index"})
    _configure(tmp_path, keyword="file", vector="vec")

    results, notes = search(tmp_path, "launch", limit=10, registry=_registry(paths))

    assert results
    assert any("kuzu segfaulted" in note for note in notes)


def test_the_agent_tool_reports_degradation_and_audits_it(tmp_path: Path) -> None:
    import json

    from jigga.core.models import AgentConfig, WorkflowStep
    from jigga.runtime.capabilities import CapabilityManifest
    from jigga.runtime.handlers import _search_memory_handler
    from jigga.runtime.runtime_context import RuntimeContext

    paths = init_runtime(tmp_path)
    _memory(paths)
    _configure(tmp_path, keyword="file", vector="ghost")

    runtime = RuntimeContext(agent=AgentConfig(id="w", name="W", role="r"), home=tmp_path,
                             logs_dir=paths.logs, sessions_dir=paths.sessions)
    manifest = CapabilityManifest(name="memory", version="1", summary="", actions=["memory.search"])
    out = _search_memory_handler(WorkflowStep(id="s", action="memory.search"), manifest,
                                 {"query": "launch"}, {}, runtime)

    assert out["results"]
    assert any("ghost" in note for note in out["degraded"])
    events = [json.loads(line) for line
              in (paths.logs / "events.jsonl").read_text().splitlines() if line.strip()]
    assert [e for e in events if e["type"] == "memory.search.degraded"]

# JIGGA Memory Backends

## Purpose

`MEMORY_MODEL.md` defines *what* JIGGA remembers (raw, structured, summaries,
indexed) and *who* can see it (scopes). This document defines *how* the
indexed and derived layers are stored and queried — the **pluggable backend
architecture** for keyword, vector, and graph retrieval.

Design inputs:
- [Ajay on graph memory cost architecture](https://x.com/ajay4ai/status/2086543628181410244) — extraction vs reasoning, cache the stable prefix, batch backfills, temporal edges, validate before write.
- [starmex on the five-layer AI stack](https://x.com/starmexxx/status/2083468826390270401) — knowledge graph = Layer 2 (context), agent graph = Layer 5 (orchestration); "the verifier is the bottleneck, not the model."

## Principles

1. **File-first stays canonical.** `memory/raw/` and `memory/structured/` remain the ground truth. All indexes and graphs are **derived** — rebuildable from files, diff-able, deletable without data loss.
2. **Default is file-only.** Zero-config installs behave exactly as today (FTS5 keyword index over the memory tree). Vector and graph are strictly opt-in.
3. **Pluggable via the same pattern JIGGA already uses.** Secrets and sandboxing already use `backend: auto | file | keychain | env` (Milestone E). Memory backends follow the same shape.
4. **Vendor-neutral through JIGGA-native protocols.** No third-party library appears in JIGGA's public interface. Graphiti, LanceDB, Kuzu, Neo4j — all live behind `KeywordIndex` / `VectorIndex` / `GraphIndex` protocols.
5. **Every backend honors scope + sensitivity.** `MemoryScope.includes/excludes`, `restricted=True`, and the proposal-queue write gate apply uniformly. A backend that can't honor these is not eligible to ship.
6. **Every write path passes verifiers.** Machine-checkable primitives — not "looks good" model self-review — gate every insertion into any backend.
7. **Cost/latency measured on the real bill.** JIGGA's default runtime is Codex/OpenAI, not Anthropic. Any cost claim in this doc references OpenAI/local pricing.

## Configuration surface

```yaml
# ~/.jigga/config.yaml
memory:
  backends:
    keyword: file            # 'file' (default, existing FTS5) | 'none'
    vector: none             # 'none' (default) | 'local' | 'lancedb'
    graph:  none             # 'none' (default) | 'graphiti' | 'kuzu-native'

  # Driver-specific config only present when the corresponding backend is selected.
  vector_local:
    model: BAAI/bge-small-en-v1.5   # local embedding model, no API cost per query
    store: memory/indexes/vectors.sqlite

  graphiti:
    store: kuzu              # 'kuzu' (embedded, default) | 'falkordb' | 'neo4j'
    embedder: local          # match the vector backend or run standalone
    extraction:
      provider: default      # honors the agent's configured model router
      tier: cheap            # 'cheap' | 'mid' | 'top' — Ajay's separation
      batch_backfill: true   # OpenAI Batch API for historical episodes
```

**Backwards compatibility:** an install with no `memory.backends` block behaves as today — file backend, FTS5 keyword index, no vector, no graph.

## How a backend gets in: the provider seam (shipped)

A capability pack has always been able to add **actions** — tools an agent
calls. That is the wrong shape for a memory backend, which the runtime consults
on its own: written to when memory lands, read from when memory is searched.
Neither is a tool call.

So a manifest can also declare what it **provides**:

```yaml
name: memory-graphiti
version: 0.1.0
summary: Temporal knowledge-graph memory via Graphiti over embedded Kuzu.
type: native
provides:
  memory.graph: memory_graphiti.backend:GraphitiIndex
actions: [memory.query_graph, memory.subgraph, memory.facts_for_episode]
setup:
  - ["pip", "install", "graphiti-core", "kuzu"]
```

`runtime/providers.py` resolves `module:attr` exactly as `dispatcher.resolve_handler`
resolves an action — same import mechanism, same trust boundary, same approval
gate. Only **approved** capabilities are consulted: naming a pack in config is
not enough to get its code imported.

A manifest that provides an implementation no longer has to declare actions.
A pure backend has no agent-facing tool, and requiring one would mean inventing
a tool nobody calls.

**This is what keeps core at stdlib + PyYAML.** Graphiti pulls in a graph
database and an embedding model; the pack owns those and installs them in its
own `setup:`, so an install that never enables graph memory never pays for them.

### Retrieval fusion (shipped)

`runtime/memory_router.search()` queries every configured backend and fuses the
rankings with reciprocal rank fusion (`1/(k + rank)`, k=60). RRF needs no score
calibration between backends — comparing a BM25 score to a cosine similarity is
meaningless, and normalising them would invent a precision neither has. Each hit
records which backends found it, because agreement is itself signal.

With nothing configured the router returns the keyword backend's results
**identically** — not equivalently. A router that re-scored a single backend's
output would have changed behaviour for every existing install.

An optional backend that is missing, un-approved, dependency-less or throwing
degrades the search and says so: results still come back, the reason rides along
as `degraded`, and the agent path audits `memory.search.degraded`. A backend that
vanished quietly would have someone reading keyword-only results believing their
graph answered.

## Backend protocols

Defined in `jigga/runtime/memory/backends/base.py` (proposed). All backends are pure Python interfaces; drivers implement them.

```python
class MemoryBackend(Protocol):
    """Any backend that can accept memory episodes."""
    def write_episode(self, entry: dict) -> str: ...
    def health(self) -> dict: ...

class KeywordIndex(Protocol):
    """Existing FTS5 path implements this; today's search_memory() becomes a thin router."""
    def search(self, query: str, *, scope: str | None,
               team: str | None, limit: int) -> list[Hit]: ...

class VectorIndex(Protocol):
    """Semantic similarity. Local embedding preferred (no per-query cost)."""
    def upsert(self, doc_id: str, text: str, meta: dict) -> None: ...
    def search(self, query: str, *, k: int,
               scope: str | None, team: str | None) -> list[Hit]: ...
    def delete(self, doc_id: str) -> None: ...

class GraphIndex(Protocol):
    """Typed entity/edge store with temporal validity."""
    def ingest_episode(self, episode_id: str, text: str,
                       occurred_at: str) -> IngestResult: ...
    def query(self, question: str, *, k: int,
              temporal_as_of: str | None = None) -> list[Fact]: ...
    def subgraph(self, entity: str, hops: int = 2,
                 temporal_as_of: str | None = None) -> Subgraph: ...
    def facts_for(self, episode_id: str) -> list[Fact]: ...
```

Common result shapes (`Hit`, `Fact`, `Subgraph`) live in
`jigga/runtime/memory/backends/types.py`. Every backend returns the same
shapes so `search_memory()` can fuse across all three.

## Drivers

### `file` (keyword, default)

- What today's `memory_index.py` already does.
- SQLite FTS5 over `raw/` + `structured/` + `summaries/` + team/role workspaces.
- BM25 ranking, graceful fallback to tokenized scan when FTS5 is missing.
- **No changes required** — the existing implementation gets wrapped in the `KeywordIndex` protocol.

### `vector_local` (vector, opt-in)

- Local embedding via `sentence-transformers` (default: `bge-small-en-v1.5`).
- Storage: `sqlite-vec` extension or a bundled small vector store.
- **No API cost per query** — matches JIGGA's local-first principle.
- Upsert on memory write; delete on compaction.
- Scope/team filtering happens at query time on the metadata column.

### `vector_lancedb` (vector, opt-in, larger installs)

- LanceDB embedded, on-disk columnar vector store.
- Better ergonomics for >1M documents or when metadata-heavy filtering is common.
- Same protocol; same tool surface.

### `graphiti` (graph, opt-in, recommended)

- Wraps [getzep/graphiti](https://github.com/getzep/graphiti).
- Temporal-first: every fact has `valid_from` / `valid_until` — solves the temporal-edges gap from the audit.
- Provenance via episodes — matches JIGGA's `memory/raw/` model exactly.
- Custom ontology via Pydantic entity/edge types.
- Hybrid retrieval built-in (semantic + keyword + graph traversal).
- BYO graph DB underneath: Kuzu (embedded, default), FalkorDB, or Neo4j.
- MCP server available — external Codex/Claude sessions can query JIGGA's graph memory as a tool.

**Ontology hooks:** JIGGA defines base entity/edge types (`Person`, `Project`, `Tool`, `Team`, `Preference`, plus `works_on`, `depends_on`, `prefers`, etc.). Extensions add types via a plugin protocol so third-party skills can enrich the ontology without patching JIGGA.

**Extraction pipeline (Ajay-aligned):**
1. New `raw/` entry lands.
2. Extraction driver reads it, calls the configured provider at **cheap tier** by default.
3. Stable schema prefix (byte-identical across calls) exploits OpenAI automatic prompt caching (~50% off cached input on identical prefixes ≥1024 tokens).
4. Output JSON hits `verifiers.py`: `json_parse` → `required_fields` → `temporal_edge` → `array_items`.
5. Sensitive types route through the existing `memory_proposals` queue.
6. Approved facts get written to Graphiti with `source: raw/<id>.json` provenance.
7. Historical backfill uses OpenAI Batch API (~50% off, 24h SLA, stackable with cache).

### `kuzu-native` (graph, opt-in, minimalist)

- Thin JIGGA-native wrapper around embedded Kuzu — no Graphiti opinions.
- For users who want an entity/edge store without adopting Graphiti's ontology model.
- Same `GraphIndex` protocol; simpler extraction pipeline (JIGGA-defined schema only).
- **Trade-off:** loses Graphiti's temporal-fact machinery — JIGGA implements it or accepts weaker temporal support.

## Tool surface

Each backend registers agent-callable tools via the existing capability system. Agents call whichever their scope grants; permissions gate access.

| Tool | Backend required | Purpose |
|---|---|---|
| `memory.search` | keyword | Existing keyword search (unchanged). |
| `memory.search_semantic` | vector | Semantic neighborhood — "find things that mean something similar." |
| `memory.search_hybrid` | keyword + vector (+ graph if present) | Fused ranking — the recommended default when multiple backends are enabled. |
| `memory.query_graph` | graph | Natural-language question answered by graph traversal + reasoning. |
| `memory.subgraph` | graph | Retrieve N-hop subgraph around an entity, temporally filtered. |
| `memory.facts_for_episode` | graph | Show what facts were derived from a given `raw/` episode — audit trail. |

All tools:
- Honor `MemoryScope.includes/excludes`.
- Honor `restricted=True` for group/shared sessions (no leaking private layers).
- Route through the same audit log (`memory.read` events).

## Verifier gate on every write path

Per starmex ("the verifier is the bottleneck, not the model"), no data enters any backend without passing machine-checkable primitives:

- Structured facts: `json_parse` → `required_fields([subject, relation, object])` → `temporal_edge` → `enum(relation, allowed_relations)`.
- Graph edges: as above, plus dedupe against existing edges.
- Sensitive types (`fact` / `preference` / `relationship`): existing `memory_proposals` queue continues to gate on human approval.

Verifier primitives live in `jigga/runtime/verifiers.py` (proposed sibling to the ClawRecipes port), pure functions, no I/O, deterministic. Same shape as the accepted ClawRecipes verifier module.

## Retrieval fusion

When multiple backends are active, `search_memory()` becomes a router:

```
question
   │
   ├─► KeywordIndex.search    (BM25 hits)
   ├─► VectorIndex.search     (semantic hits)
   └─► GraphIndex.query       (fact hits + subgraph)
        │
   fusion: dedupe by source id, rank by weighted score, cap by scope budget
        │
   Hit[] to caller (agent tool response OR context_pack.recent layer)
```

Fusion strategy stays simple: reciprocal-rank fusion (RRF) as default; teams can override via config.

## Migration path

1. ✅ **Router + provider seam** (`runtime/memory_router.py`, `runtime/providers.py`).
   Today's FTS5 path is the keyword backend, reached through the router; a pack
   supplies any other. No behavior change with nothing configured.
2. **Introduce `backends/base.py`** with the protocols.
3. **Add `backends/vector_local.py`** behind the flag. Upsert on `raw/` write, delete on compaction.
4. **Add `backends/graph_graphiti.py`** behind the flag. Extraction pipeline lands with verifier gate.
5. **Update `search_memory()`** to route/fuse based on configured backends.
6. **Docs:** deprecate direct FTS5 references in favor of `KeywordIndex`.

Each step ships independently; each step is reversible; nothing forces existing installs to change.

## Non-goals

- Building an agent-graph orchestration framework (starmex's Layer 5). JIGGA already has workflows and handoffs — that layer is done.
- Replacing the file-first canonical layer.
- Locking JIGGA to any single graph DB, vector store, or embedding provider.
- Silent behavior changes for existing installs — every new backend is opt-in.

## Baseline: what we inject today (J-001)

The JIGGA side of ClawRecipes ticket 0272, shipped as instrumentation rather
than a spike: `assemble_agent_context()` records what each layer contributed and
what the caps took away, on the existing `agent.context.assembled` audit event.
`jigga memory injection-report [--days N] [--json]` summarises a window.

Measured per invocation: estimated tokens injected, per-layer available vs
included characters, which layers were clipped by their own cap and which were
dropped entirely once the 9,000-char total ran out, the stable/volatile split
(the prefix-cache-eligible share), and how many pinned facts and team learnings
existed versus how many the `_RECENT_FACTS` window let through.

**First reading (2026-08-18, seven agents on the maintainer's live config):**

```
tokens injected   p50 780   p95 1001   max 1001
hit the 9k total cap   0 runs (0.0%)
prefix-cacheable       75.7% of injected chars
team facts seen        learnings 3/3 · pinned 0/0
```

This changes the case for graph memory rather than supporting it. The caps are
**not** binding — runs land around a third of the budget, nothing is dropped,
and the recency window is returning every fact that exists. So the argument for
a graph here cannot be "the prompt is starving"; it has to be recall quality on
a memory corpus large enough to matter, which this install does not yet have.
Re-read the report once memory has grown before committing to the build. The
one number already worth acting on is the 24% of injected characters that sit
below the volatile boundary and are re-billed at full price on every call.

## Open questions

1. **Ontology governance.** Who owns the base entity/edge types? Recommendation: JIGGA core owns a minimal set; skills extend via a documented plugin protocol.
2. **Local embedding model default.** `bge-small-en-v1.5` (400MB) vs `all-MiniLM-L6-v2` (80MB) — trade-off between quality and install size.
3. **Vector store engine.** `sqlite-vec` (matches existing FTS5 pattern) vs LanceDB (better at scale). Default `vector: local` uses `sqlite-vec`; `vector: lancedb` for heavier installs.
4. **Graphiti extraction cost on our real Codex bill.** Requires a spike to measure — same spike as ClawRecipes ticket 0273, adapted for JIGGA episodes. Not yet run: it needs 500 real extractions against a paid model and a local Kuzu/Neo4j install, both of which are decisions rather than code.

## Referenced work

- ClawRecipes PR #299 — verifier primitives (TypeScript). JIGGA-side port lands as `jigga/runtime/verifiers.py`.
- ClawRecipes tickets 0272 / 0273 — plugin-side measurement + spike, complementary to JIGGA J-001 / J-004.
- Ajay's article — the extraction/reasoning cost split and cache-the-prefix pattern.
- starmex's article — the five-layer map and the verifier bottleneck.

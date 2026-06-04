"""Keyword search over memory (Milestone D, slice D1; coverage extended #96).

Indexes into a sqlite **FTS5** table under `memory/indexes/` and answers
`search_memory(query, scope)` with ranked snippets. The index rebuilds on demand
and when stale (a source file changed since the last build). If FTS5 isn't
compiled into sqlite, it falls back to a plain tokenized scan — same results,
just no persistent index.

Coverage (#96 — the Hermes contract: the resident context baseline (#86) stays
small because everything beyond it is one zero-token search away):
- memory tree: `raw/` entries + `structured/` + `summaries/`
- **task history**: live + archived task records — every channel chat is a
  task, so "what did RJ ask me yesterday" is searchable
- team workspaces: team/pinned memory, role MEMORY.md, **dated daily logs**,
  **agent-outputs**, **status notes**, and the **handoff decision log**

This makes retrieval scale as memory grows (the Milestone D goal) and powers the
`memory.search` capability + `jigga memory search`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from jigga.core.config import load_memory_scopes
from jigga.core.io import ensure_dir

INDEX_NAME = "memory.fts.sqlite"
_TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl"}


def index_path(memory_dir: Path) -> Path:
    return Path(memory_dir) / "indexes" / INDEX_NAME


def _render_task(path: Path) -> str:
    """A task record as searchable text: title + description (channel chats
    carry the message text here) + assignee/state/channel breadcrumbs."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    meta = data.get("metadata") or {}
    bits = [str(data.get("title") or ""), str(data.get("description") or ""),
            str(data.get("assignee") or ""), str(data.get("state") or ""),
            str(meta.get("channel") or ""), str(meta.get("sender") or "")]
    return " ".join(b for b in bits if b).strip()


def _render_raw(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    content = data.get("content")
    rendered = content if isinstance(content, str) else json.dumps(content, default=str)
    return f"{data.get('type', '')} {rendered} {json.dumps(data.get('source') or {}, default=str)}".strip()


def _iter_documents(memory_dir: Path) -> Iterator[tuple[str, Path, str]]:
    """(layer, path, text) for every indexable memory file."""
    memory_dir = Path(memory_dir)
    raw = memory_dir / "raw"
    if raw.exists():
        for path in sorted(raw.glob("*.json")):
            text = _render_raw(path)
            if text:
                yield ("raw", path, text)
    for layer in ("structured", "summaries"):
        directory = memory_dir / layer
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix.lower() in _TEXT_SUFFIXES:
                try:
                    yield (layer, path, path.read_text(encoding="utf-8"))
                except OSError:
                    continue
    # Task history (#96) — live + archived (compaction moves old finished tasks
    # to tasks/archive/, which is where old channel chats end up).
    tasks_dir = memory_dir.parent / "tasks"
    for directory in (tasks_dir, tasks_dir / "archive"):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            text = _render_task(path)
            if text:
                yield ("tasks", path, text)
    # Per-team / per-role memory lives in the team workspaces (D2). Indexed with
    # a `team:<id>` / `role:<member>` layer so search can be scoped to one team.
    workspaces = memory_dir.parent / "workspaces"
    if workspaces.exists():
        for team_dir in sorted(p for p in workspaces.iterdir() if p.is_dir()):
            team_files = [team_dir / "shared-context" / "memory" / "team.jsonl",
                          team_dir / "shared-context" / "memory" / "pinned.jsonl",
                          team_dir / "shared-context" / "handoffs.jsonl",   # decision log (#96)
                          team_dir / "notes" / "status.md",                  # ops log (#96)
                          team_dir / "notes" / "plan.md"]
            outputs = team_dir / "shared-context" / "agent-outputs"          # deliverables (#96)
            if outputs.exists():
                team_files += sorted(p for p in outputs.iterdir() if p.suffix.lower() in _TEXT_SUFFIXES)
            for path in team_files:
                if path.is_file():
                    try:
                        yield (f"team:{team_dir.name}", path, path.read_text(encoding="utf-8"))
                    except OSError:
                        continue
            roles = team_dir / "roles"
            if roles.exists():
                for role_dir in sorted(p for p in roles.iterdir() if p.is_dir()):
                    role_files = [role_dir / "MEMORY.md"]
                    daily = role_dir / "memory"                              # dated logs (#96)
                    if daily.exists():
                        role_files += sorted(daily.glob("*.md"))
                    for path in role_files:
                        if path.is_file():
                            try:
                                yield (f"role:{role_dir.name}", path, path.read_text(encoding="utf-8"))
                            except OSError:
                                continue


def fts_available() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE _t USING fts5(x)")
        conn.close()
        return True
    except sqlite3.OperationalError:
        return False


def _max_source_mtime(memory_dir: Path) -> float:
    times = [path.stat().st_mtime for _, path, _ in _iter_documents(memory_dir)]
    return max(times) if times else 0.0


def rebuild_index(memory_dir: Path) -> int:
    """(Re)build the FTS5 index from scratch. Returns the document count."""
    memory_dir = Path(memory_dir)
    ensure_dir(memory_dir / "indexes")
    path = index_path(memory_dir)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE VIRTUAL TABLE docs USING fts5(layer, path, text)")
        count = 0
        for layer, doc_path, text in _iter_documents(memory_dir):
            conn.execute("INSERT INTO docs(layer, path, text) VALUES (?, ?, ?)", (layer, str(doc_path), text))
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()


def _is_stale(memory_dir: Path) -> bool:
    path = index_path(memory_dir)
    if not path.exists():
        return True
    return _max_source_mtime(memory_dir) > path.stat().st_mtime


def _fts_query(query: str) -> str:
    """Quote each term so user input can't break FTS5 syntax (terms AND-ed)."""
    tokens = re.findall(r"[A-Za-z0-9_]+", query or "")
    return " ".join(f'"{token}"' for token in tokens)


def _scope_roots(memory_dir: Path, scope_id: str) -> list[Path] | None:
    scope = load_memory_scopes(memory_dir).get(scope_id)
    if scope is None:
        return None
    return [(Path(memory_dir) / inc.removeprefix("memory/")).resolve() for inc in scope.includes]


def _in_scope(path: str, roots: list[Path]) -> bool:
    resolved = Path(path).resolve()
    return any(resolved == root or root in resolved.parents for root in roots)


# `tasks` is global like the memory tree: every agent may recall its own task
# history, and tasks aren't team-namespaced on disk. Team/role layers stay
# team-gated; sensitive narrowing uses `scope` (and group sessions are already
# restricted at the context level).
_GLOBAL_LAYERS = ("raw", "structured", "summaries", "tasks")


def _team_visible(layer: str, team: str) -> bool:
    """Global memory is visible to everyone; team/role memory only to its team."""
    return layer in _GLOBAL_LAYERS or layer == f"team:{team}" or layer.startswith(f"role:{team}")


def _scan_search(memory_dir: Path, query: str, *, scope: str | None, team: str | None, limit: int) -> list[dict[str, Any]]:
    """FTS5-free fallback: rank by how many query terms a doc contains."""
    tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9_]+", query or "")]
    if not tokens:
        return []
    roots = _scope_roots(memory_dir, scope) if scope else None
    scored: list[dict[str, Any]] = []
    for layer, path, text in _iter_documents(memory_dir):
        if team is not None and not _team_visible(layer, team):
            continue
        if roots is not None and not _in_scope(str(path), roots):
            continue
        lowered = text.lower()
        hits = sum(lowered.count(token) for token in tokens)
        if hits:
            scored.append({"layer": layer, "path": str(path), "snippet": text[:200].strip(), "score": -float(hits)})
    scored.sort(key=lambda r: r["score"])
    return scored[:limit]


def search_memory(
    memory_dir: Path, query: str, *, scope: str | None = None, team: str | None = None,
    limit: int = 10, rebuild: bool = False,
) -> list[dict[str, Any]]:
    """Ranked keyword search over memory. `scope` restricts to that scope's
    `includes`; `team` restricts to global memory + that team's/roles' memory.
    Returns [{layer, path, snippet, score}] (lower score = better)."""
    memory_dir = Path(memory_dir)
    if not fts_available():
        return _scan_search(memory_dir, query, scope=scope, team=team, limit=max(1, limit))
    fts = _fts_query(query)
    if not fts:
        return []
    if rebuild or _is_stale(memory_dir):
        rebuild_index(memory_dir)
    # Over-fetch when filtering so the post-filter still returns `limit`.
    fetch = max(1, limit) * (5 if (scope or team) else 1)
    conn = sqlite3.connect(index_path(memory_dir))
    try:
        rows = conn.execute(
            "SELECT layer, path, snippet(docs, 2, '[', ']', ' … ', 12), bm25(docs) "
            "FROM docs WHERE docs MATCH ? ORDER BY bm25(docs) LIMIT ?",
            (fts, fetch),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    results = [{"layer": r[0], "path": r[1], "snippet": r[2], "score": r[3]} for r in rows]
    if team is not None:
        results = [r for r in results if _team_visible(r["layer"], team)]
    if scope is not None:
        roots = _scope_roots(memory_dir, scope) or []
        results = [r for r in results if _in_scope(r["path"], roots)]
    return results[:limit]

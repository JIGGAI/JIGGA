"""Memory compaction (Milestone D, slice D3).

Keeps memory bounded as it grows — the milestone's exit condition. Periodically:
- **archive old raw entries** — `memory/raw/*.json` past `raw_retention_days` →
  `memory/raw/archive/` (out of the live set + the search index).
- **archive stale team facts** — `team.jsonl` entries past `fact_stale_days` →
  `team.archive.jsonl` (kept on disk, dropped from search). `pinned.jsonl` is
  never compacted — that's the curated, keep-forever set.
- **archive old finished tasks** — completed/failed tasks past
  `task_retention_days` → `tasks/archive/`.

Runs on the supervisor heartbeat at most once per `interval_hours` (a marker
file guards the cadence), and on demand via `jigga memory compact`.

Config (`config.yaml`):
    memory:
      compaction:
        enabled: true
        interval_hours: 24
        raw_retention_days: 30
        fact_stale_days: 90
        task_retention_days: 30
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jigga.core.config import load_runtime_config
from jigga.core.io import append_jsonl, ensure_dir, read_jsonl, rewrite_jsonl, write_json
from jigga.core.models import now_iso
from jigga.runtime.tasks import forget_tasks

_MARKER = ".compaction.json"
_DEFAULTS = {"enabled": True, "interval_hours": 24, "raw_retention_days": 30,
             "fact_stale_days": 90, "task_retention_days": 30}


def _config(home: Path) -> dict[str, Any]:
    raw = (load_runtime_config(home).get("memory") or {}).get("compaction") or {}
    return {**_DEFAULTS, **raw}


def _parse(ts: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _older(ts: Any, cutoff: datetime) -> bool:
    parsed = _parse(ts)
    return parsed is not None and parsed < cutoff  # unparseable → keep (never archive ambiguous)


def _archive_raw(memory_dir: Path, cutoff: datetime, dry_run: bool) -> list[str]:
    raw = memory_dir / "raw"
    moved: list[str] = []
    for path in sorted(raw.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if _older(data.get("time"), cutoff):
            moved.append(path.name)
            if not dry_run:
                dest = raw / "archive"
                ensure_dir(dest)
                shutil.move(str(path), str(dest / path.name))
    return moved


def _archive_team_facts(home: Path, cutoff: datetime, dry_run: bool) -> int:
    workspaces = home / "workspaces"
    archived = 0
    if not workspaces.exists():
        return 0
    for team_dir in sorted(p for p in workspaces.iterdir() if p.is_dir()):
        live = team_dir / "shared-context" / "memory" / "team.jsonl"
        if not live.exists():
            continue
        # Tolerant read: one corrupt/half-written line must not crash the whole
        # compaction pass (which runs on the supervisor heartbeat).
        entries = read_jsonl(live)
        keep = [e for e in entries if not _older(e.get("time"), cutoff)]
        stale = [e for e in entries if _older(e.get("time"), cutoff)]
        if not stale:
            continue
        archived += len(stale)
        if not dry_run:
            archive = team_dir / "shared-context" / "memory" / "team.archive.jsonl"
            for entry in stale:
                append_jsonl(archive, entry)
            rewrite_jsonl(live, keep)
    return archived


def _archive_tasks(tasks_dir: Path, cutoff: datetime, dry_run: bool) -> list[str]:
    moved: list[str] = []
    if not tasks_dir.exists():
        return moved
    for path in sorted(tasks_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("state") in {"completed", "failed"} and _older(data.get("updated_at"), cutoff):
            moved.append(path.name)
            if not dry_run:
                dest = tasks_dir / "archive"
                ensure_dir(dest)
                shutil.move(str(path), str(dest / path.name))
    return moved


def compact_memory(home: Path, *, now: datetime | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Run a compaction pass. Returns what was (or would be) archived."""
    home = Path(home)
    cfg = _config(home)
    now = now or datetime.now(timezone.utc)
    raw_archived = _archive_raw(home / "memory", now - timedelta(days=int(cfg["raw_retention_days"])), dry_run)
    facts_archived = _archive_team_facts(home, now - timedelta(days=int(cfg["fact_stale_days"])), dry_run)
    tasks_archived = _archive_tasks(home / "tasks", now - timedelta(days=int(cfg["task_retention_days"])), dry_run)
    if tasks_archived and not dry_run:
        # archived task files move out of the store — drop them from the index
        forget_tasks(home / "tasks", tasks_archived)
    return {"dry_run": dry_run, "raw_archived": raw_archived,
            "facts_archived": facts_archived, "tasks_archived": tasks_archived}


def _marker_path(home: Path) -> Path:
    return home / "memory" / _MARKER


def maybe_compact(home: Path, *, now: datetime | None = None) -> dict[str, Any] | None:
    """Compact at most once per `interval_hours` (marker-guarded). Returns the
    summary if it ran, else None. Called on the supervisor heartbeat."""
    home = Path(home)
    cfg = _config(home)
    if not cfg.get("enabled", True):
        return None
    now = now or datetime.now(timezone.utc)
    marker = _marker_path(home)
    if marker.exists():
        try:
            last = _parse(json.loads(marker.read_text(encoding="utf-8")).get("last_run"))
        except (OSError, ValueError):
            last = None
        if last is not None and now - last < timedelta(hours=float(cfg["interval_hours"])):
            return None
    summary = compact_memory(home, now=now)
    ensure_dir(marker.parent)
    write_json(marker, {"last_run": now_iso()})
    return summary

"""Channel transcripts — conversation continuity for EXTERNAL channels.

Webchat is file-backed, so its transcript came free (`inbox/outbox.jsonl`) and
threads got context windows + rolling summaries (#128/#129). External channels
(Telegram today; Slack / iMessage later) keep their messages on someone else's
server — inbound became a task and was forgotten, outbound went straight to
the wire — so every conversation ran amnesiac.

This module is the generic local transcript those channels record into:

    channels/<channel>/transcript.jsonl            one line per message, both directions
    channels/<channel>/summaries/<conv>.json       rolling summary (watermarked)
    channels/<channel>/archive/transcript-*.jsonl  age-based archival (daily sweep)

Recording points (the contract a channel implements):
  inbound  — the listener records every gate-passing message for any adapter
             that doesn't declare `self_transcribed` (webchat does — its inbox
             IS the transcript)
  outbound — the channel's send primitive records after a successful send
             (covers both agent tool replies and runtime notices)

The adapter then exposes the standard `thread_context` hook delegating to
`thread_context_block` here — the agent loop (#128) already probes for it, so
no agent-side changes are needed. Window/summary semantics, config knobs
(`channels.<name>.context_turns` / `summarize` / `retention_days`), and the
fold prompt all mirror webchat's so prompts look identical across channels.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, read_json, write_json
from jigga.core.models import now_iso
from jigga.runtime.audit import append_event, new_id

DEFAULT_CONTEXT_TURNS = 12
CONTEXT_CHAR_CAP = 4000
DEFAULT_RETENTION_DAYS = 30

_SUMMARY_SYSTEM = (
    "You maintain a rolling summary of one chat conversation between a user and "
    "an assistant. Fold the new turns into the existing summary: keep names, "
    "decisions, numbers, and open questions; drop pleasantries. Reply with ONLY "
    "the updated summary, under 150 words."
)


def _slug(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", raw).strip("._") or "channel"


def _channel_dir(home: Path, channel: str) -> Path:
    return Path(home) / "channels" / _slug(str(channel))


def transcript_path(home: Path, channel: str) -> Path:
    return _channel_dir(home, channel) / "transcript.jsonl"


def _summary_path(home: Path, channel: str, conversation_id: Any) -> Path:
    raw = str(conversation_id)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return _channel_dir(home, channel) / "summaries" / f"{_slug(raw)[:60]}-{digest}.json"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def record(home: Path, channel: str, *, conversation_id: Any, sender: str, text: str,
           direction: str, message_id: Any = None) -> dict[str, Any]:
    """Append one message to the channel's transcript. `sender` is the display
    name rendered into the agent's context (`you`-equivalents for humans,
    "agent" for replies) — in group chats every participant keeps their name."""
    entry = {
        "id": new_id("ctm"),
        "conversation_id": str(conversation_id),
        "sender": str(sender or "user"),
        "text": str(text),
        "direction": "out" if direction == "out" else "in",
        "ts": now_iso(),
    }
    if message_id is not None:
        entry["message_id"] = message_id
    path = transcript_path(home, channel)
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def history(home: Path, channel: str, conversation_id: Any, *, limit: int = 200) -> list[dict[str, Any]]:
    entries = [e for e in _read_jsonl(transcript_path(home, channel))
               if e.get("conversation_id") == str(conversation_id)]
    entries.sort(key=lambda e: str(e.get("ts") or ""))
    return entries[-limit:]


def _channel_cfg(home: Path, channel: str) -> dict[str, Any]:
    from jigga.core.config import load_runtime_config

    cfg = (load_runtime_config(home).get("channels") or {}).get(str(channel))
    return cfg if isinstance(cfg, dict) else {}


def _context_turns(cfg: dict[str, Any]) -> int:
    try:
        return int(cfg.get("context_turns", DEFAULT_CONTEXT_TURNS))
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_TURNS


def thread_tail(home: Path, channel: str, conversation_id: Any, *,
                exclude_message_id: Any = None) -> str:
    """The conversation's recent verbatim tail (`sender: text`, oldest first),
    excluding the message currently being handled (it's the task body)."""
    turns = _context_turns(_channel_cfg(home, channel))
    if turns <= 0:
        return ""
    exclude = None if exclude_message_id is None else str(exclude_message_id)
    entries = [e for e in history(home, channel, conversation_id, limit=turns + 1)
               if exclude is None or str(e.get("message_id")) != exclude][-turns:]
    text = "\n".join(f"{e.get('sender') or 'user'}: {e.get('text') or ''}" for e in entries)
    if len(text) > CONTEXT_CHAR_CAP:
        text = text[-CONTEXT_CHAR_CAP:]
        cut = text.find("\n")           # drop the partial oldest line
        if 0 <= cut < len(text) - 1:
            text = text[cut + 1:]
    return text


def load_summary(home: Path, channel: str, conversation_id: Any) -> dict[str, Any]:
    path = _summary_path(home, channel, conversation_id)
    try:
        data = read_json(path) if path.exists() else {}
    except Exception:  # noqa: BLE001 — corrupt summary → start over, never break chat
        return {}
    return data if isinstance(data, dict) else {}


def roll_summary(home: Path, logs_dir: Path, channel: str, conversation_id: Any, *,
                 agent_id: str = "system.channel") -> str:
    """Conversational compaction, identical semantics to webchat's (#129):
    lazily fold the turns that scrolled out of the window into the rolling
    summary, watermarked by the last folded entry id so each call folds only
    new overflow. Failure keeps the previous summary + watermark."""
    from jigga.runtime.model_router import ModelCallItem, ModelCallRequest, call_model

    cfg = _channel_cfg(home, channel)
    record_doc = load_summary(home, channel, conversation_id)
    existing = str(record_doc.get("summary") or "")
    turns = _context_turns(cfg)
    if turns <= 0 or not cfg.get("summarize", True):
        return existing
    entries = history(home, channel, conversation_id, limit=100_000)
    overflow = entries[:-turns] if len(entries) > turns else []
    if not overflow:
        return existing
    ids = [e.get("id") for e in overflow]
    watermark = record_doc.get("through_id")
    start = ids.index(watermark) + 1 if watermark in ids else 0
    fresh = overflow[start:]
    if not fresh:
        return existing
    rendered = "\n".join(f"{e.get('sender') or 'user'}: {e.get('text') or ''}" for e in fresh)
    user = (f"Existing summary (may be empty):\n{existing or '(none)'}\n\n"
            f"New turns to fold in (oldest first):\n{rendered[:CONTEXT_CHAR_CAP * 2]}")
    request = ModelCallRequest(
        agent_id=agent_id, role="conversation summarizer",
        task={"id": f"{channel}-summary:{conversation_id}"},
        items=[ModelCallItem(id="system", role="system", content=_SUMMARY_SYSTEM),
               ModelCallItem(id="turns", role="user", content=user)],
    )
    result = call_model(home, logs_dir, request)
    summary = (result.content or "").strip()
    if result.status != "ok" or not summary:
        return existing
    path = _summary_path(home, channel, conversation_id)
    ensure_dir(path.parent)
    write_json(path, {"conversation_id": str(conversation_id), "summary": summary,
                      "through_id": fresh[-1].get("id"), "updated": now_iso()})
    append_event(logs_dir, "channel.summary_rolled", channel=str(channel),
                 conversation=str(conversation_id), folded_turns=len(fresh))
    return summary


def thread_context_block(home: Path, channel: str, conversation_id: Any, *,
                         exclude_message_id: Any = None,
                         logs_dir: Path | None = None,
                         agent_id: str | None = None) -> str:
    """The rendered context block (summary above tail) — what a channel
    adapter's `thread_context` hook returns to the agent loop. Same headers
    as webchat's so prompts look identical across channels."""
    tail = thread_tail(home, channel, conversation_id, exclude_message_id=exclude_message_id)
    if not tail:
        return ""
    if logs_dir is not None:
        try:
            summary = roll_summary(home, logs_dir, channel, conversation_id,
                                   agent_id=agent_id or "system.channel")
        except Exception:  # noqa: BLE001 — summary is an enhancement, never a blocker
            summary = str(load_summary(home, channel, conversation_id).get("summary") or "")
    else:
        summary = str(load_summary(home, channel, conversation_id).get("summary") or "")
    parts = []
    if summary:
        parts.append(f"## Earlier in this conversation (summary)\n{summary}")
    parts.append(f"## Recent conversation in this thread (oldest first)\n{tail}")
    return "\n\n".join(parts)


def archive_transcripts_for(home: Path, channel: str, *, now: Any = None,
                            dry_run: bool = False) -> int:
    """Move transcript lines older than `channels.<channel>.retention_days`
    (default 30; 0 disables) to `archive/transcript-YYYY-MM.jsonl`. Far
    simpler than webchat's inbox (no consume offset — the transcript is
    context-only): an age-bounded prefix move. Unparsable lines count as old.
    Summaries are never touched, so dormant threads keep continuity."""
    cfg = _channel_cfg(home, channel)
    try:
        retention = int(cfg.get("retention_days", DEFAULT_RETENTION_DAYS))
    except (TypeError, ValueError):
        retention = DEFAULT_RETENTION_DAYS
    if retention <= 0:
        return 0
    path = transcript_path(home, channel)
    if not path.exists():
        return 0
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=retention)).isoformat()
    lines = path.read_text(encoding="utf-8").splitlines()
    prefix = 0
    for line in lines:
        try:
            ts = str((json.loads(line) or {}).get("ts") or "")
        except (json.JSONDecodeError, AttributeError):
            ts = ""
        if ts >= cutoff:
            break
        prefix += 1
    if not prefix or dry_run:
        return prefix
    archive_dir = _channel_dir(home, channel) / "archive"
    ensure_dir(archive_dir)
    by_month: dict[str, list[str]] = {}
    for line in lines[:prefix]:
        try:
            month = str((json.loads(line) or {}).get("ts") or "")[:7] or "unknown"
        except (json.JSONDecodeError, AttributeError):
            month = "unknown"
        by_month.setdefault(month, []).append(line)
    for month, chunk in by_month.items():
        with (archive_dir / f"transcript-{month}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("\n".join(chunk) + "\n")
    survivors = lines[prefix:]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(("\n".join(survivors) + "\n") if survivors else "", encoding="utf-8")
    tmp.replace(path)
    return prefix


def archive_all(home: Path, *, now: Any = None, dry_run: bool = False) -> dict[str, int]:
    """Age out every external channel's transcript (daily compaction sweep).
    Channels are discovered by their transcript files; webchat's inbox/outbox
    have their own offset-aware archival and never appear here."""
    home = Path(home)
    results: dict[str, int] = {}
    channels_dir = home / "channels"
    if not channels_dir.is_dir():
        return results
    for child in sorted(channels_dir.iterdir()):
        if not (child / "transcript.jsonl").exists():
            continue
        archived = archive_transcripts_for(home, child.name, now=now, dry_run=dry_run)
        if archived:
            results[child.name] = archived
    return results

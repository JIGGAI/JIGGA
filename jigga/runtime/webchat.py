"""Webchat channel — the browser as a JIGGA channel (M2, jiggaview's Chat page).

Same contract as Telegram, file-backed instead of HTTP: inbound messages are
appended to `channels/webchat/inbox.jsonl` (by `jigga webchat send` — the
jiggaview chat route shells it), the adapter's poll consumes them past a
stored offset, they ride the NORMAL channel pipeline (identity → task → agent
runs), and the agent replies with the `webchat.send_message` TOOL, which
appends `outbox.jsonl` — what the browser renders. Everything is a file:
auditable, greppable, replayable.

Latency: `jigga webchat send --wait` ingests inline and returns the reply in
the same invocation (synchronous chat UX); the supervisor's normal channel
poll is the backstop for anything sent without --wait (offset consumption
prevents double-processing). No secrets, no allowlist — webchat is reachable
only by whoever can reach the jiggaview host (RJ: tailnet is the moat).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir, read_json, write_json
from jigga.core.models import now_iso
from jigga.runtime.audit import append_event, new_id

SUPPORTED_ACTIONS = ("webchat.send_message", "webchat.poll_messages")
DEFAULT_CONVERSATION = "web"
# Thread-context injection: how many recent turns of the conversation ride
# along with each agent run (config `channels.webchat.context_turns`; 0
# disables), and the hard char budget the rendered turns may occupy in the
# prompt (~1k tokens). Models are stateless — JIGGA is the chat client, so it
# re-sends the recent tail the way every chat app does invisibly.
DEFAULT_CONTEXT_TURNS = 12
CONTEXT_CHAR_CAP = 4000


def _dir(home: Path) -> Path:
    return Path(home) / "channels" / "webchat"


def _inbox(home: Path) -> Path:
    return _dir(home) / "inbox.jsonl"


def _outbox(home: Path) -> Path:
    return _dir(home) / "outbox.jsonl"


def _offset_path(home: Path) -> Path:
    return Path(home) / "state" / "webchat_offset.json"


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


def _append_jsonl(path: Path, entry: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_offset(home: Path) -> int:
    path = _offset_path(home)
    try:
        return int(read_json(path).get("offset", 0)) if path.exists() else 0
    except Exception:  # noqa: BLE001 — corrupt offset → reprocess (idempotent-ish, never lose)
        return 0


def store_offset(home: Path, offset: int, *, anchor_id: str | None = None) -> None:
    """`anchor_id` is the id of the LAST CONSUMED entry — it lets the offset
    self-heal after archival rewrites the inbox (line positions shift; the
    anchor's new index is re-found). None = nothing consumed yet."""
    ensure_dir(_offset_path(home).parent)
    write_json(_offset_path(home), {"offset": int(offset), "anchor_id": anchor_id})


def _resolve_offset(home: Path, entries: list[dict[str, Any]]) -> int:
    """The effective consume position, healed against the anchor.

    When the stored anchor (last consumed id) is present in the current file,
    its index + 1 IS the offset — regardless of what the numeric offset says,
    which makes any crash ordering around an archival trim safe. Anchor gone
    (the whole consumed prefix was archived) → 0, and everything still in the
    file is unconsumed by construction (archival only ever trims consumed
    lines). Legacy offset files without an anchor keep the raw number."""
    path = _offset_path(home)
    try:
        raw = read_json(path) if path.exists() else {}
    except Exception:  # noqa: BLE001 — corrupt offset → reprocess, never lose
        return 0
    if not isinstance(raw, dict):
        return 0
    try:
        offset = int(raw.get("offset", 0))
    except (TypeError, ValueError):
        return 0
    anchor = raw.get("anchor_id")
    if not anchor:
        return max(0, offset)
    for index in range(len(entries) - 1, -1, -1):
        if entries[index].get("id") == anchor:
            return index + 1
    return 0


def append_inbound(home: Path, text: str, *, conversation_id: str = DEFAULT_CONVERSATION,
                   sender: str = "you", target_agent: str | None = None) -> dict[str, Any]:
    """The browser's send: append an inbound message (seq = line position).

    `target_agent` addresses a specific agent instead of the channel's default
    — the chat page's agent picker. Carried on the entry, surfaced as the
    event's `target` so the listener routes to it (webchat is local/trusted;
    remote channels like Telegram deliberately get no sender-chosen routing)."""
    entry = {
        "id": new_id("wcm"),
        "conversation_id": str(conversation_id),
        "sender": str(sender),
        "text": str(text),
        "ts": now_iso(),
    }
    if target_agent:
        entry["target_agent"] = str(target_agent)
    _append_jsonl(_inbox(home), entry)
    return entry


def poll_messages(home: Path, *, long_poll_seconds: int = 0, limit: int = 50) -> dict[str, Any]:
    """Adapter contract: new inbound messages past the offset, normalized like
    telegram's (`chat_id`/`sender`/`text`/`message_id`), offset advanced.
    `long_poll_seconds` is accepted but ignored — webchat is a local file, the
    --wait send path gives instant UX, and blocking the supervisor's
    sequential channel loop would starve the other channels."""
    home = Path(home)
    entries = _read_jsonl(_inbox(home))
    offset = _resolve_offset(home, entries)
    fresh = entries[offset:offset + max(1, limit)]
    if fresh:
        consumed_through = offset + len(fresh)
        store_offset(home, consumed_through,
                     anchor_id=entries[consumed_through - 1].get("id"))
    messages = [
        {
            "chat_id": entry.get("conversation_id") or DEFAULT_CONVERSATION,
            "chat_type": "private",
            "sender": entry.get("sender") or "you",
            "sender_id": entry.get("sender") or "you",
            "text": entry.get("text") or "",
            "message_id": entry.get("id"),
            "mentions_bot": False,
            "target_agent": entry.get("target_agent"),
        }
        for entry in fresh
    ]
    return {"status": "ok", "messages": messages}


def send_message(home: Path, conversation_id: Any, text: str) -> dict[str, Any]:
    """Outbound: append to the outbox the browser renders."""
    entry = {
        "id": new_id("wcr"),
        "conversation_id": str(conversation_id or DEFAULT_CONVERSATION),
        "sender": "agent",
        "text": str(text),
        "ts": now_iso(),
    }
    _append_jsonl(_outbox(home), entry)
    return {"status": "ok", "message_id": entry["id"]}


def history(home: Path, *, conversation_id: str = DEFAULT_CONVERSATION,
            limit: int = 200) -> list[dict[str, Any]]:
    """The merged conversation, chronological — what the chat page renders."""
    merged = [e for e in _read_jsonl(_inbox(home)) + _read_jsonl(_outbox(home))
              if (e.get("conversation_id") or DEFAULT_CONVERSATION) == conversation_id]
    merged.sort(key=lambda e: str(e.get("ts") or ""))
    return merged[-limit:]


def _summary_path(home: Path, conversation_id: Any) -> Path:
    """Per-conversation summary file. Conversation ids are free-form (agent
    ids, --conversation values), so the filename is a sanitized slug plus a
    short content hash — collision-safe and traversal-proof."""
    raw = str(conversation_id)
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", raw).strip("._") or "conversation"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return _dir(home) / "summaries" / f"{slug[:60]}-{digest}.json"


def load_summary(home: Path, conversation_id: Any) -> dict[str, Any]:
    path = _summary_path(home, conversation_id)
    try:
        data = read_json(path) if path.exists() else {}
    except Exception:  # noqa: BLE001 — corrupt summary → start over, never break chat
        return {}
    return data if isinstance(data, dict) else {}


_SUMMARY_SYSTEM = (
    "You maintain a rolling summary of one chat conversation between a user and "
    "an assistant. Fold the new turns into the existing summary: keep names, "
    "decisions, numbers, and open questions; drop pleasantries. Reply with ONLY "
    "the updated summary, under 150 words."
)


def roll_summary(home: Path, logs_dir: Path, conversation_id: Any, *,
                 agent_id: str = "system.webchat") -> str:
    """Conversational compaction (lazy, at chat time): fold the turns that have
    scrolled out of the context window into this conversation's rolling
    summary, then return it. The watermark (`through_message_id`) makes folds
    incremental — each call folds only what's newly overflowed, so a long
    thread costs one small model call per message, not a re-summarization.
    A model failure keeps the previous summary and watermark (retried on the
    next message). Stored per-conversation under `channels/webchat/summaries/`
    — conversation STATE lives with the conversation; durable knowledge still
    flows to agent memory via the normal run breadcrumbs."""
    from jigga.core.config import load_runtime_config
    from jigga.runtime.model_router import ModelCallItem, ModelCallRequest, call_model

    cfg = (load_runtime_config(home).get("channels") or {}).get("webchat") or {}
    record = load_summary(home, conversation_id)
    existing = str(record.get("summary") or "")
    try:
        turns = int(cfg.get("context_turns", DEFAULT_CONTEXT_TURNS))
    except (TypeError, ValueError):
        turns = DEFAULT_CONTEXT_TURNS
    if turns <= 0 or not cfg.get("summarize", True):
        return existing
    entries = history(home, conversation_id=str(conversation_id), limit=100_000)
    overflow = entries[:-turns] if len(entries) > turns else []
    if not overflow:
        return existing
    ids = [e.get("id") for e in overflow]
    watermark = record.get("through_message_id")
    start = ids.index(watermark) + 1 if watermark in ids else 0
    fresh = overflow[start:]
    if not fresh:
        return existing
    rendered = "\n".join(f"{e.get('sender') or 'you'}: {e.get('text') or ''}" for e in fresh)
    user = (f"Existing summary (may be empty):\n{existing or '(none)'}\n\n"
            f"New turns to fold in (oldest first):\n{rendered[:CONTEXT_CHAR_CAP * 2]}")
    request = ModelCallRequest(
        agent_id=agent_id, role="conversation summarizer",
        task={"id": f"webchat-summary:{conversation_id}"},
        items=[ModelCallItem(id="system", role="system", content=_SUMMARY_SYSTEM),
               ModelCallItem(id="turns", role="user", content=user)],
    )
    result = call_model(home, logs_dir, request)
    summary = (result.content or "").strip()
    if result.status != "ok" or not summary:
        return existing
    path = _summary_path(home, conversation_id)
    ensure_dir(path.parent)
    write_json(path, {"conversation_id": str(conversation_id), "summary": summary,
                      "through_message_id": fresh[-1].get("id"), "updated": now_iso()})
    append_event(logs_dir, "webchat.summary_rolled", conversation=str(conversation_id),
                 folded_turns=len(fresh), through=fresh[-1].get("id"))
    return summary


def thread_context(home: Path, conversation_id: Any, *,
                   exclude_message_id: str | None = None) -> str:
    """The conversation's recent tail, rendered for the agent's prompt —
    `sender: text` lines, oldest first. Excludes the message currently being
    handled (it's already the task body). Capped by `context_turns` config
    (0 disables) and a char budget that drops the OLDEST overflow — the newest
    turns are the ones a follow-up question refers to."""
    from jigga.core.config import load_runtime_config

    cfg = (load_runtime_config(home).get("channels") or {}).get("webchat") or {}
    try:
        turns = int(cfg.get("context_turns", DEFAULT_CONTEXT_TURNS))
    except (TypeError, ValueError):
        turns = DEFAULT_CONTEXT_TURNS
    if turns <= 0:
        return ""
    entries = [e for e in history(home, conversation_id=str(conversation_id), limit=turns + 1)
               if e.get("id") != exclude_message_id][-turns:]
    text = "\n".join(f"{e.get('sender') or 'you'}: {e.get('text') or ''}" for e in entries)
    if len(text) > CONTEXT_CHAR_CAP:
        text = text[-CONTEXT_CHAR_CAP:]
        cut = text.find("\n")           # drop the partial oldest line
        if 0 <= cut < len(text) - 1:
            text = text[cut + 1:]
    return text


DEFAULT_RETENTION_DAYS = 30


def _archivable_prefix(path: Path, cutoff_iso: str,
                       *, max_entries: int | None = None) -> tuple[list[str], int, int]:
    """(all raw lines, count of leading archivable lines, parsed entries in
    that prefix). PREFIX only — entries append chronologically, so survivors
    keep contiguous positions and the anchored offset can re-find itself.
    Unparsable lines count as old (junk rides to the archive rather than
    pinning the file). `max_entries` bounds the prefix in PARSED-entry space
    (the offset counts parsed entries, not raw lines — corrupt lines must not
    let archival eat an unconsumed message)."""
    if not path.exists():
        return [], 0, 0
    lines = path.read_text(encoding="utf-8").splitlines()
    count = parsed = 0
    for line in lines:
        entry: Any = None
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            entry = None
        is_entry = isinstance(entry, dict)
        if is_entry and max_entries is not None and parsed >= max_entries:
            break  # the next real entry is unconsumed — never archive it
        ts = str(entry.get("ts") or "") if is_entry else ""
        if ts >= cutoff_iso:
            break
        count += 1
        if is_entry:
            parsed += 1
    return lines, count, parsed


def _append_archive(home: Path, stem: str, lines: list[str]) -> None:
    """Append archived lines grouped by entry month (`archive/<stem>-YYYY-MM.jsonl`)."""
    by_month: dict[str, list[str]] = {}
    for line in lines:
        try:
            month = str((json.loads(line) or {}).get("ts") or "")[:7] or "unknown"
        except (json.JSONDecodeError, AttributeError):
            month = "unknown"
        by_month.setdefault(month, []).append(line)
    archive_dir = _dir(home) / "archive"
    ensure_dir(archive_dir)
    for month, chunk in by_month.items():
        with (archive_dir / f"{stem}-{month}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("\n".join(chunk) + "\n")


def archive_transcripts(home: Path, *, now: Any = None, dry_run: bool = False) -> dict[str, Any]:
    """Housekeeping: move transcript entries older than
    `channels.webchat.retention_days` (default 30; 0 disables) into
    `channels/webchat/archive/` — still files, still greppable, just out of
    the hot path. Inbox lines must ALSO be consumed (index < offset): an
    unread message is never archived, however old. Thread continuity for a
    dormant conversation survives via its rolling summary file, which
    archival never touches.

    Crash-safe by anchor, not by ordering: archive-append, then store the
    rebased offset (with the surviving last-consumed anchor), then rewrite
    the trimmed file via temp+rename. A crash between any two steps leaves
    the anchor resolvable, so the next poll heals — worst case is duplicate
    lines in the cold archive, never a lost or skipped message."""
    from datetime import datetime, timedelta, timezone

    from jigga.core.config import load_runtime_config

    cfg = (load_runtime_config(home).get("channels") or {}).get("webchat") or {}
    try:
        retention = int(cfg.get("retention_days", DEFAULT_RETENTION_DAYS))
    except (TypeError, ValueError):
        retention = DEFAULT_RETENTION_DAYS
    summary = {"archived_inbox": 0, "archived_outbox": 0, "dry_run": dry_run}
    if retention <= 0:
        return summary
    moment = now or datetime.now(timezone.utc)
    cutoff = (moment - timedelta(days=retention)).isoformat()

    # Inbox: prefix bounded by the consumed offset (in parsed-entry space).
    entries = _read_jsonl(_inbox(home))
    offset = _resolve_offset(home, entries)
    lines, prefix, parsed_in_prefix = _archivable_prefix(_inbox(home), cutoff, max_entries=offset)
    if prefix:
        summary["archived_inbox"] = prefix
        if not dry_run:
            _append_archive(home, "inbox", lines[:prefix])
            survivors = lines[prefix:]
            remaining = _read_jsonl_lines(survivors)
            rebased = max(0, offset - parsed_in_prefix)
            anchor = remaining[rebased - 1].get("id") if rebased > 0 else None
            store_offset(home, rebased, anchor_id=anchor)
            _rewrite_lines(_inbox(home), survivors)

    # Outbox: age is the only condition (replies have no consume semantics).
    lines, prefix, _ = _archivable_prefix(_outbox(home), cutoff)
    if prefix:
        summary["archived_outbox"] = prefix
        if not dry_run:
            _append_archive(home, "outbox", lines[:prefix])
            _rewrite_lines(_outbox(home), lines[prefix:])
    return summary


def _read_jsonl_lines(lines: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _rewrite_lines(path: Path, lines: list[str]) -> None:
    """Replace a transcript file's contents atomically (temp + rename)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
    tmp.replace(path)


def list_conversations(home: Path) -> list[dict[str, Any]]:
    """Every distinct conversation across inbox+outbox, newest-activity first —
    what the chat page's thread list renders. `agent` is the thread's targeted
    agent (from the picker); None means the channel-default thread. Order-
    independent: "last" is decided by timestamp comparison, not file order."""
    convs: dict[str, dict[str, Any]] = {}
    for entry in _read_jsonl(_inbox(home)) + _read_jsonl(_outbox(home)):
        cid = str(entry.get("conversation_id") or DEFAULT_CONVERSATION)
        conv = convs.setdefault(cid, {
            "conversation_id": cid, "count": 0, "agent": None,
            "last_ts": "", "last_text": "", "last_sender": "",
        })
        conv["count"] += 1
        if entry.get("target_agent"):
            conv["agent"] = entry["target_agent"]
        ts = str(entry.get("ts") or "")
        if ts >= conv["last_ts"]:
            conv["last_ts"] = ts
            conv["last_text"] = (entry.get("text") or "")[:120]
            conv["last_sender"] = str(entry.get("sender") or "")
    return sorted(convs.values(), key=lambda c: c["last_ts"], reverse=True)


def webchat_handler(step, _capability, resolved_input, _memory_context, runtime) -> Any:
    """Capability handler: `webchat.send_message` is how an agent replies in
    the browser (poll is runtime-only — the ingest pipeline owns it)."""
    params = resolved_input if isinstance(resolved_input, dict) else {}
    if step.action == "webchat.send_message":
        text = params.get("text")
        if text is None:
            raise ValueError("webchat.send_message requires 'text' in input")
        result = send_message(runtime.home, params.get("chat_id") or params.get("conversation_id"),
                              str(text))
        result.setdefault("source", "capability.webchat")
        result.setdefault("action", step.action)
        return result
    raise ValueError(f"Unknown webchat action: {step.action!r}. "
                     f"Supported: {', '.join(SUPPORTED_ACTIONS)}.")


class WebchatAdapter:
    """Webchat on the ChannelAdapter contract (registered in channels.ADAPTERS)."""

    name = "webchat"
    long_polls = False  # local file read — returns instantly; never paces the supervisor loop
    self_transcribed = True  # inbox/outbox.jsonl IS the transcript — the listener must not double-record

    def poll(self, home: Path, *, long_poll_seconds: int = 0) -> dict[str, Any]:
        from jigga.runtime.channels import TelegramAdapter  # shared event mapper

        result = poll_messages(home, long_poll_seconds=long_poll_seconds)
        events = []
        for message in result.get("messages", []):
            event = TelegramAdapter.to_event(message)
            event.source = "webchat"
            if message.get("target_agent"):
                # Sender-chosen routing (the chat page's agent picker) — the
                # listener validates the agent exists before honoring it.
                event.target = {"agent": message["target_agent"]}
            events.append(event)
        return {"status": result.get("status", "ok"), "events": events, "raw": result}

    def send(self, home: Path, *, conversation_id: Any, text: str) -> dict[str, Any]:
        return send_message(home, conversation_id, text)

    def thread_context(self, home: Path, *, conversation_id: Any,
                       exclude_message_id: str | None = None,
                       logs_dir: Path | None = None,
                       agent_id: str | None = None) -> str:
        """Optional adapter hook: channels with a local transcript provide the
        conversation context block for the agent's prompt — the rolling
        summary of everything that scrolled out of the window (when logs_dir
        is given, overflow is folded first; the model call needs the audit
        log) above the verbatim recent tail. The agent loop probes for this
        via getattr — channels without one (telegram has no local transcript
        store) simply don't inject."""
        tail = thread_context(home, conversation_id, exclude_message_id=exclude_message_id)
        if not tail:
            return ""
        if logs_dir is not None:
            try:
                summary = roll_summary(home, logs_dir, conversation_id,
                                       agent_id=agent_id or "system.webchat")
            except Exception:  # noqa: BLE001 — summary is an enhancement, never a blocker
                summary = str(load_summary(home, conversation_id).get("summary") or "")
        else:
            summary = str(load_summary(home, conversation_id).get("summary") or "")
        parts = []
        if summary:
            parts.append(f"## Earlier in this conversation (summary)\n{summary}")
        parts.append(f"## Recent conversation in this thread (oldest first)\n{tail}")
        return "\n\n".join(parts)

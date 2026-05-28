from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from jigga.core.io import ensure_dir
from jigga.core.models import now_iso


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def append_event(logs_dir: Path, event_type: str, status: str = "ok", **details: Any) -> dict[str, Any]:
    ensure_dir(logs_dir)
    event = {
        "id": new_id("evt"),
        "time": now_iso(),
        "type": event_type,
        "status": status,
        "details": details,
    }
    with (logs_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    return event

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from jigga.core.models import now_iso
from jigga.runtime.audit import new_id


@dataclass
class JiggaEvent:
    id: str
    type: str
    source: str
    created_at: str = field(default_factory=now_iso)
    targets: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, event_type: str, source: str, targets: list[str] | None = None, **payload: Any) -> "JiggaEvent":
        return cls(id=new_id("event"), type=event_type, source=source, targets=targets or [], payload=payload)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

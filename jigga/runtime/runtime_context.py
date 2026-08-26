from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jigga.core.models import AgentConfig, WorkflowStep
from jigga.runtime.capabilities import CapabilityManifest

@dataclass(frozen=True)
class RuntimeContext:
    """Runtime plumbing passed to capability handlers, kept separate from
    `memory_context` so memory-derived output never accidentally captures
    paths or runtime references."""

    agent: AgentConfig | None
    home: Path
    logs_dir: Path
    sessions_dir: Path
    # The workspace this run is bound to. Relative filesystem paths resolve
    # against it, so `shared-context/plan.md` means the same thing to an agent
    # as it does in its own prompt. None (workflows, triggers) keeps the old
    # process-cwd behaviour.
    workspace_id: str | None = None


Handler = Callable[
    [WorkflowStep, CapabilityManifest, Any, dict[str, Any], RuntimeContext],
    Any,
]

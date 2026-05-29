"""Capability handlers used by the dispatcher extensibility test.

A user-local capability manifest can declare
`handler: tests.fixtures.capability_handlers:custom_handler` and the runtime
imports + dispatches without modifying jigga itself.
"""

from __future__ import annotations

from typing import Any


def custom_handler(step, capability, resolved_input, memory_context, runtime) -> dict[str, Any]:
    return {
        "marker": "custom_handler_was_called",
        "capability": capability.name,
        "action": step.action,
        "input": resolved_input,
        "agent_id": runtime.agent.id if runtime.agent is not None else None,
    }

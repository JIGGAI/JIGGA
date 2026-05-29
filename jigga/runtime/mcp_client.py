"""Minimal MCP (Model Context Protocol) stdio client.

Used by the `mcp_server` capability handler to dispatch workflow actions to
external MCP-style servers. Supports the subset needed for a single tool call:

  1. Send `initialize` request (id=1).
  2. Send `notifications/initialized` notification.
  3. Send `tools/call` request (id=2) with the action name + arguments.
  4. Read responses until we see id=2; return its `result`.

Transport is newline-delimited JSON over the subprocess stdin/stdout. The
implementation deliberately uses `subprocess.run` with `input=...` and a
single read of `stdout` so we don't have to manage async streams; a real MCP
client would interleave reads and writes, but for one tool call the batched
exchange is sufficient and avoids race-prone polling.

Spec reference: https://modelcontextprotocol.io/
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from jigga.runtime.sandbox import SandboxSpec, run_sandboxed

MCP_PROTOCOL_VERSION = "2024-11-05"


def _build_messages(tool_name: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "jigga", "version": "0.1.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    ]


def _parse_responses(stdout: str) -> list[dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            responses.append(json.loads(line))
        except json.JSONDecodeError:
            # Servers occasionally write diagnostic noise to stdout; ignore
            # non-JSON lines rather than crashing the whole exchange.
            continue
    return responses


def call_mcp_tool(
    spec: SandboxSpec,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch a single MCP tool call against a stdio server.

    The subprocess invocation (env allowlist, cwd, timeout, argv) is described
    by the SandboxSpec; this function owns only the MCP framing (JSON-RPC
    messages over stdio and response parsing).

    Returns the `result` payload from the `tools/call` response. Raises
    `RuntimeError` on protocol errors, non-zero exit codes whose stdout
    contains no usable response, or timeouts.
    """
    arguments = arguments or {}
    messages = _build_messages(tool_name, arguments)
    input_text = "\n".join(json.dumps(message) for message in messages) + "\n"

    try:
        completed = run_sandboxed(spec, input=input_text)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"MCP server {spec.command} timed out after {spec.timeout_seconds}s"
        ) from exc

    responses = _parse_responses(completed.stdout)
    tool_response = next((r for r in responses if r.get("id") == 2), None)
    if tool_response is None:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no output"
        raise RuntimeError(
            f"MCP server {spec.command} did not respond to tools/call "
            f"(exit_code={completed.returncode}): {detail[:500]}"
        )
    if "error" in tool_response:
        raise RuntimeError(f"MCP server returned error: {tool_response['error']}")
    return tool_response.get("result") or {}

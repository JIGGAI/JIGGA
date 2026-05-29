#!/usr/bin/env python3
"""Minimal MCP server for end-to-end demo and testing.

Speaks JSON-RPC over stdio. Implements just enough of the MCP protocol for
JIGGA's mcp_server capability handler to drive it:

  - `initialize` — respond with protocol version + server info.
  - `notifications/initialized` — accept silently.
  - `tools/call` — handle `demo.echo` and `demo.upper`, return a content block.

A real MCP server would also implement `tools/list`, `resources/list`,
streaming responses, etc. This demo deliberately stays in one file so the
shape is obvious to read.
"""

from __future__ import annotations

import json
import sys
from typing import Any

MCP_PROTOCOL_VERSION = "2024-11-05"


def _write(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _initialize_result(msg_id: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "jigga-demo-mcp", "version": "0.1.0"},
        },
    }


def _tool_call_result(msg_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name", "")
    arguments = params.get("arguments") or {}
    if name == "demo.echo":
        text = f"echo: {json.dumps(arguments)}"
    elif name == "demo.upper":
        text = str(arguments.get("input", "")).upper()
    else:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"Unknown tool: {name}"},
        }
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": False,
        },
    }


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        msg_id = message.get("id")
        if method == "initialize":
            _write(_initialize_result(msg_id))
        elif method == "notifications/initialized":
            continue
        elif method == "tools/call":
            _write(_tool_call_result(msg_id, message.get("params") or {}))
        elif msg_id is not None:
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not implemented: {method}"},
                }
            )


if __name__ == "__main__":
    main()

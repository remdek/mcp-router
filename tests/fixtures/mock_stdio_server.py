#!/usr/bin/env python3
"""Minimal MCP stdio server for transport tests (supports oneshot + persistent)."""

from __future__ import annotations

import json
import os
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo tool via stdio transport",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo"}},
        },
    },
    {
        "name": "set_token",
        "description": "Store a token in process memory (persistent-session test)",
        "inputSchema": {
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
        },
    },
    {
        "name": "get_token",
        "description": "Read the token stored by set_token",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pid",
        "description": "Return this process pid",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_STATE: dict[str, str | None] = {"token": None}


def _handle(req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "mock-stdio", "version": "0.0.1"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "set_token":
            _STATE["token"] = str(args.get("token", ""))
            text = "set"
        elif name == "get_token":
            text = str(_STATE["token"])
        elif name == "pid":
            text = str(os.getpid())
        else:
            text = "stdio-ok"
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": text}]},
        }
    return None


def main() -> None:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        request = json.loads(line)
        if request.get("method") == "notifications/initialized":
            continue
        response = _handle(request)
        if response:
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()

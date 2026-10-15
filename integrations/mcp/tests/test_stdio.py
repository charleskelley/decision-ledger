"""The real entry point serves MCP on stdio, with protocol messages only.

``python -m decisionledger_mcp`` runs as a subprocess against a ledger URL
nothing listens on: the lifecycle and ``tools/list`` need no ledger, and a
tool call reports the unreachable ledger as a tool error. Every stdout line
is a JSON-RPC message.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def test_the_stdio_server_answers_the_lifecycle_and_lists_its_tools():
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "lookup_decision",
                "arguments": {"decision_id": "0f3a9d1e-7a2b-4c3d-9e8f-1a2b3c4d5e6f"},
            },
        },
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    completed = subprocess.run(  # noqa: S603 (fixed interpreter and module)
        [sys.executable, "-m", "decisionledger_mcp"],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "DECISIONLEDGER_URL": "http://127.0.0.1:9",
            "DECISIONLEDGER_KEY": "k",
        },
        check=True,
    )

    replies = {r["id"]: r for r in map(json.loads, completed.stdout.splitlines())}
    assert sorted(replies) == [1, 2, 3]
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[1]["result"]["serverInfo"]["name"] == "decisionledger"
    names = [t["name"] for t in replies[2]["result"]["tools"]]
    assert names == [
        "record_decision",
        "gate_decision",
        "lookup_decision",
        "replay_decision",
        "list_decisions_for_entity",
        "resolve_hold",
    ]
    call = replies[3]["result"]
    assert call["isError"] is True
    assert call["structuredContent"]["code"] == "LEDGER_UNAVAILABLE"

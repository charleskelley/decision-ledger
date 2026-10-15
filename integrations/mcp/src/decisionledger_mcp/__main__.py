"""Serve a DecisionLedger as MCP tools on stdio.

Configuration (environment):

- ``DECISIONLEDGER_URL`` — the ledger service (default
  ``http://localhost:8000``).
- ``DECISIONLEDGER_KEY`` — the API key the tools act with: a reasoner's key
  (record, gate, and read that reasoner's decisions) or the operator key
  (read every decision).
- ``DECISIONLEDGER_OPERATOR_KEY`` — optional; sent on ``resolve_hold`` only.

For example, in an MCP client's server list::

    {
        "command": "uv",
        "args": ["run", "decisionledger-mcp"],
        "env": {
            "DECISIONLEDGER_URL": "http://localhost:8000",
            "DECISIONLEDGER_KEY": "<a reasoner's key>",
        },
    }

stdout carries protocol messages only; logs go to stderr.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import structlog

from app.client import HttpClient
from decisionledger_mcp.server import McpServer, serve_stdio
from decisionledger_mcp.tools import ledger_tools

#: Server version reported at ``initialize``.
VERSION = "0.1.0"

INSTRUCTIONS = """\
DecisionLedger is a decision system of record with a gating service. Record \
a decision you made under one of your registered bypass rules \
(record_decision), or ask the ledger's gates to decide (gate_decision); act \
on the returned decision_action, not on your proposal. Every decision is an \
immutable bundle you can read (lookup_decision), replay (replay_decision), \
and find by subject (list_decisions_for_entity). A held decision is resolved \
by a human (resolve_hold, operator key)."""


def build_server(client: HttpClient) -> McpServer:
    """The MCP server over ``client``."""
    return McpServer(
        ledger_tools(client),
        name="decisionledger",
        version=VERSION,
        instructions=INSTRUCTIONS,
    )


async def _run() -> None:
    client = HttpClient(
        os.environ.get("DECISIONLEDGER_URL", "http://localhost:8000"),
        api_key=os.environ.get("DECISIONLEDGER_KEY") or None,
        operator_key=os.environ.get("DECISIONLEDGER_OPERATOR_KEY") or None,
    )
    try:
        await serve_stdio(build_server(client))
    finally:
        await client.aclose()


def main() -> int:
    """Run the stdio server until stdin closes."""
    # stdout is the protocol stream: every log line goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())

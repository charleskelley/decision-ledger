"""DecisionLedger as seven MCP tools, for agents (DR-31, DR-34).

``python -m decisionledger_mcp`` (or the ``decisionledger-mcp`` script)
serves the ledger over the Model Context Protocol on stdio. Its tools —
``record_decision``, ``gate_decision``, ``lookup_decision``,
``replay_decision``, ``verify_decision``, ``list_decisions_for_entity``,
``resolve_hold`` — call
a ledger service through ``app.client.HttpClient``, with the key in
``DECISIONLEDGER_KEY`` (and ``DECISIONLEDGER_OPERATOR_KEY`` for
resolutions), so an agent passes the same checks as any reasoner service.

Modules:
    server:   The hand-rolled MCP server (JSON-RPC 2.0 over stdio, tools only).
    tools:    The seven tools over a ``LedgerClient``.
    __main__: The stdio entry point (environment → client → server).
"""

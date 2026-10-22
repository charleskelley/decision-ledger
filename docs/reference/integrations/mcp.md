# `decisionledger_mcp`

The MCP server, shipped as the `decisionledger-mcp` uv workspace member under
`integrations/mcp/` (import package `decisionledger_mcp`, console script
`decisionledger-mcp`). It serves the ledger as seven Model Context Protocol
tools on stdio (`record_decision`, `gate_decision`, `lookup_decision`,
`replay_decision`, `verify_decision`, `list_decisions_for_entity`,
`resolve_hold`), so an agent can record and gate its decisions, read them
back, and verify the evidence they relied on. It is a client of the
ledger, not a plugin: every tool calls a ledger service through
[`HttpClient`][app.client.HttpClient] with the key in `DECISIONLEDGER_KEY`
(and `DECISIONLEDGER_OPERATOR_KEY` for resolutions), so an agent passes the
same checks as any reasoner service.

The server is hand-rolled (JSON-RPC 2.0 over stdio, tools only) rather than
built on an MCP SDK; its tool descriptions are written for agents.

::: decisionledger_mcp
    options:
      show_root_heading: false

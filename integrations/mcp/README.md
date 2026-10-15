# decisionledger-mcp

DecisionLedger as six [Model Context Protocol](https://modelcontextprotocol.io)
tools, so an agent can record and gate its decisions in the ledger and read
them back. A uv workspace member; import package `decisionledger_mcp`;
distribution `decisionledger-mcp`. It is a client of the ledger: it calls a
ledger service through `app.client.HttpClient` and publishes no plugin.

| Tool | What it does |
| --- | --- |
| `record_decision` | Record a decision the agent made under one of its registered bypass rules (mandatory gates only) |
| `gate_decision` | Ask the ledger's gates to decide (mandatory and advisory gates) |
| `lookup_decision` | Read a decision's full bundle |
| `replay_decision` | Re-run enforcement on a decision's stored evidence |
| `list_decisions_for_entity` | A subject's decisions, newest first |
| `resolve_hold` | A human's terminal resolution of a held decision (operator key) |

The server is hand-rolled (JSON-RPC 2.0 over stdio, tools only, no MCP SDK)
and speaks protocol versions 2024-11-05 through 2025-11-25. The tool
descriptions are written for agents: in particular, what each
`reasoner_context` field means for an LLM-agent reasoner (model id, prompt
digest, tool-trace digest), so the agent does not fill them with filler.

## Run

```bash
DECISIONLEDGER_URL=http://localhost:8000 \
DECISIONLEDGER_KEY=<a reasoner's API key> \
uv run decisionledger-mcp
```

`DECISIONLEDGER_OPERATOR_KEY` (optional) is sent on `resolve_hold` only.
stdout carries protocol messages only; logs go to stderr. An MCP client
configuration:

```json
{"command": "uv", "args": ["run", "decisionledger-mcp"],
 "env": {"DECISIONLEDGER_URL": "http://localhost:8000",
         "DECISIONLEDGER_KEY": "<a reasoner's API key>"}}
```

## Tests

`tests/` (no `__init__.py`) runs under the root `pytest` with no Docker:
the protocol (lifecycle, errors, batches, concurrency), each tool against a
recording client, and the real entry point as a subprocess.

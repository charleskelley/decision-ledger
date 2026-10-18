# decisionledger-mcp

DecisionLedger as six [Model Context Protocol](https://modelcontextprotocol.io)
tools, so an agent can record and gate its decisions in the ledger and read
them back. A uv workspace member; import package `decisionledger_mcp`;
distribution `decisionledger-mcp`. It is a client of the ledger: it calls a
ledger service through `app.client.HttpClient` and publishes no plugin.

| Tool | Arguments | What it does |
| --- | --- | --- |
| `record_decision` | `submission` | Record a decision the agent made under one of its registered bypass rules (mandatory gates only) |
| `gate_decision` | `submission` | Ask the ledger's gates to decide (mandatory and advisory gates) |
| `lookup_decision` | `decision_id` | Read a decision's full bundle |
| `replay_decision` | `decision_id` | Re-run enforcement on a decision's stored evidence |
| `list_decisions_for_entity` | `entity_id`; optional `reasoner_id`, `limit`, `before`, `before_id`, `include_related` | A subject's decisions, newest first |
| `resolve_hold` | `decision_id`, `resolution_action` (ALLOW or BLOCK), `resolver_id`, `note`; optional `reviewer_role`, `reference_ticket_id` | A human's terminal resolution of a held decision (operator key) |

`submission` is a wire `Submission` (schema version 2, the same JSON as
`schemas/submission-v2.json`); its `mode` may be omitted, since each tool sets
it (a submission whose `mode` names the other tool is a `WRONG_TOOL` error).
`list_decisions_for_entity` covers the ids the ledger's identity journal
merged with the subject; `include_related` names a relationship type the
deployment's identity resolver emits and adds the decisions about subjects
one such relationship away.

The server is hand-rolled (JSON-RPC 2.0 over stdio, tools only, no MCP SDK)
and speaks protocol versions 2024-11-05 through 2025-11-25. The tool
descriptions are written for agents: in particular, what each
`reasoner_context` field means for an LLM-agent reasoner (model id, prompt
digest, tool-trace digest), so the agent does not fill them with filler.

A refusal from the ledger comes back as a tool error (`isError: true`) whose
content is the ledger's problem document (`status`, stable `code`,
`detail`); invalid arguments come back as `ARGUMENTS_INVALID`, and a ledger
that cannot be reached as `LEDGER_UNAVAILABLE`. An unknown tool is a
JSON-RPC error.

## Run

```bash
DECISIONLEDGER_URL=http://localhost:8000 \
DECISIONLEDGER_KEY=<a reasoner's API key> \
uv run decisionledger-mcp
```

`DECISIONLEDGER_URL` defaults to `http://localhost:8000` (the ledger started
by `make serve`). `DECISIONLEDGER_KEY` is a reasoner's key (record, gate, and
read that reasoner's decisions) or the operator key (read every decision).
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

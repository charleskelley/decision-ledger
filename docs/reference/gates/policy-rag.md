# `policy_rag_gate`

The RAG policy gate, shipped as the `decisionledger-gate-policy-rag` uv
workspace member under `gates/policy_rag/` (import package `policy_rag_gate`,
`gate_id` `policy`). It plugs into the ledger through the
`decisionledger.gates` entry point (`policy_rag_gate.plugin:PLUGIN`) and the
universal [`GateOutput`][core.gate.GateOutput] it returns; the framework never
imports it.

For each decision it runs hybrid retrieval over the submitting reasoner's
policy corpus, renders the reasoner's versioned prompt template, asks the LLM
for a structured verdict, and returns the universal verdict with one
evidence reference per retrieved snippet. Its own typed models
(`PolicyGateSnapshot`, `PolicyGateExtra`) are stored in the output's opaque
`evidence_snapshot` and `extra`, so replay needs neither the corpus nor the
index. The gate owns its infrastructure: the `policy_rag` Postgres schema
(its DDL in `sql/`), the Elasticsearch index (mapping in `es/`, service in
the member's `compose.yaml`), the embedding and cross-encoder models, the
LLM adapters (`llm/`, the only place an LLM SDK is imported), the corpus
loader, and its eval (retrieval, faithfulness, citation, and the LLM judge).

See [Policy gate](../../design/gates/policy/index.md) for the design.

::: policy_rag_gate
    options:
      show_root_heading: false

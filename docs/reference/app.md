# `app`

Framework runtime: orchestration, gate dispatch, enforcement, audit
storage, and the ledger's HTTP surface. Imports from `core/` only; reasoners
are discovered at runtime through the `decisionledger.reasoners` entry point
group and gates through `decisionledger.gates`. The framework half of the
reasoner ↔ framework handoff lives in [`app.submit.submit`][app.submit.submit],
which `POST /api/v1/decisions` and [`LocalClient`][app.client.LocalClient]
both reach through [`create_decision`][app.api.decisions.create_decision].

The framework imports no LLM SDK: the RAG policy gate's `llm/` adapters
(`gates/policy_rag`) are the only place one (`openai`, `anthropic`) may be
imported (DR-23). Boundary enforced by `tests/test_framework_boundary.py`.

::: app
    options:
      show_root_heading: false

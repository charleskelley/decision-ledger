# `app`

Framework runtime: orchestration, gate dispatch, enforcement, audit
storage. Imports from `core/` only; reasoners are discovered at runtime
through the `decisionledger.reasoners` entry point group and gates through
`decisionledger.gates`. The framework half of the reasoner ↔ framework handoff
lives in [`app.decide.execute_decision`][app.decide.execute_decision].

The framework imports no LLM SDK: the RAG policy gate's `llm/` adapters
(`gates/policy_rag`) are the only place one (`openai`, `anthropic`) may be
imported (DR-23). Boundary enforced by `tests/test_framework_boundary.py`.

::: app
    options:
      show_root_heading: false

# `app`

Framework runtime: orchestration, retrieval, gate, enforcement, audit
storage. Imports from `core/` and (via `app/main.py` only)
`reasoner/`. The framework half of the reasoner ↔ framework handoff
lives in [`app.decide.execute_decision`][app.decide.execute_decision].

`app/llm/` is the only place an LLM SDK (`openai`, `anthropic`) may
be imported (DR-23). Boundary enforced by
`tests/test_framework_boundary.py`.

::: app
    options:
      show_root_heading: false

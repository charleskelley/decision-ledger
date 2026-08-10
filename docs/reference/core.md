# `core`

Framework contracts: domain-agnostic Pydantic types, protocols, and
pure functions. Per DR-23/DR-24, `core/` has zero infrastructure
dependencies — no Redis clients, no database drivers, no LLM SDK
imports, no FastAPI. The full surface below can be unit-tested
without Docker running.

`core/` is consumed by every other package. It never imports from
`reasoner/` or `app/`.

::: core
    options:
      show_root_heading: false

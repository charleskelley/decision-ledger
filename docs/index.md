![DecisionLedger](assets/decision-ledger-hero/decision-ledger-hero-light.svg#only-light)
![DecisionLedger](assets/decision-ledger-hero/decision-ledger-hero-dark.svg#only-dark)

DecisionLedger is a decision system of record for AI systems, with a gating
service: one immutable Decision Bundle schema for every automated decision, a
subject-centric ledger with deterministic replay, pluggable gates for the
ambiguous cases, and eval-gated release. Reasoners and gates are plugins
outside the framework. The reference reasoner (Account Takeover) is a
real-time account-takeover risk pipeline: an ML scorer whose clear cases it
records under its registered bypass rules, and whose ambiguous ones it asks
the ledger to gate through the RAG policy gate (an LLM).

## Documentation

- [**Design**](design/index.md) — Architecture, pipeline, identity, gates,
  data model, evaluation, and the DR-1–DR-32 decision record.
- [**Reasoners**](design/reasoners/index.md) — How a domain plugs into
  the ledger: a workspace member under `reasoners/` or its own service,
  submitting one wire `Submission` in process or over HTTP
  ([submission contract](design/reasoners/reasoner-handoff.md)).
- [**Gates**](design/gates/index.md) — The gate contract, gate sets, and
  [how to implement a gate](design/gates/implementation.md).
- [**Identity**](design/identity.md) — How the ledger resolves each
  decision's subject through the enterprise's resolver, and the entity
  journal.
- [**Reference**](reference/index.md) — API reference for the framework
  packages and the workspace members.
- [**Operations**](operations/secrets.md) — Local, CI, and deployment
  secrets handling.
- [**Development**](development/contributing.md) — Contributing guide,
  style conventions, local runbook.

## Source

The full source is on GitHub:
[charleskelley/decision-ledger](https://github.com/charleskelley/decision-ledger).

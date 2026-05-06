![DecisionLedger](assets/decision-ledger-hero/decision-ledger-hero-light.svg#only-light)
![DecisionLedger](assets/decision-ledger-hero/decision-ledger-hero-dark.svg#only-dark)

DecisionLedger is a model-agnostic framework for evaluating, governing, and
auditing AI decisions with deterministic enforcement and full replay
capability. The reference reasoner (Account Takeover) is a real-time
ATO/identity risk decisioning pipeline using a hybrid ML scorer plus LLM
policy gate architecture.

## Documentation

- [**Design**](design/index.md) — Architecture, pipeline, data model,
  evaluation framework, and the DR-1–DR-25 decision record.
- [**Reasoners**](design/reasoner/reasoner.md) — How a domain plugs into
  the framework via the `build_observation()` handoff.
- [**Operations**](operations/secrets.md) — Local, CI, and deployment
  secrets handling.
- [**Development**](development/contributing.md) — Contributing guide,
  style conventions, local runbook.

## Source

The full source is on GitHub:
[charleskelley/decision-ledger](https://github.com/charleskelley/decision-ledger).

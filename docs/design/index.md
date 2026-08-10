# Design

DecisionLedger separates a multi-tenant **framework** that owns runtime
orchestration, deterministic enforcement, and audit storage from
pluggable **reasoners** that own a domain's events, features, scorer,
and policy mapping. The reference reasoner is account takeover; the
framework is reasoner-agnostic.

## Where to start

- New to the project: read [Architecture](architecture.md) (C4 L1–L3
  with the directory layout and dependency rules), then
  [Pipeline](pipeline.md) for runtime flow and the latency budget.
- Touching features, scorer, or policy gate: read
  [Reasoner ↔ framework handoff](reasoners/reasoner-handoff.md) first.
  That contract is load-bearing — every reasoner-framework interaction
  flows through `build_observation()`.
- Adding a new reasoner: read [Reasoner abstraction](reasoners/index.md)
  for the `RegisteredReasoner` shape and the steps to register one.
- Working on retrieval, prompts, or eval thresholds: read
  [Evaluation](evaluation.md) and the relevant DRs in
  [Decisions](decisions.md).

## The map

| Document                                                                    | What it covers                                                                              |
|-----------------------------------------------------------------------------|---------------------------------------------------------------------------------------------|
| [Architecture](architecture.md)                                             | C4 L1–L3 diagrams, framework/reasoner directory layout, dependency rules, twelve components |
| [Pipeline](pipeline.md)                                                     | Runtime flow, fast-path thresholds, fallback paths, latency budget                          |
| [Data model](reasoners/account-takeover/data.md)                            | Event schema, Decision Bundle structure, `decisionledger.*` Postgres schema                 |
| [Interfaces](core.md)                                                       | `core/` boundary, framework contracts, FastAPI surface                                      |
| [Infrastructure](infrastructure.md)                                         | Docker Compose stack, service containers, schema bootstrap                                  |
| [Evaluation](evaluation.md)                                                 | 5D harness, dimension methodology, CI thresholds                                            |
| [Decisions](decisions.md)                                                   | DR-1 through DR-25 — every load-bearing architecture decision and its rationale             |
| [Policy corpus](gates/policy/policy-corpus.md)                              | Corpus documents, chunking strategy, jurisdiction model                                     |
| [Scenarios](reasoners/account-takeover/scenarios.md)                        | Eight named event patterns and what each exercises                                          |
| [Gates](gates/implementation.md)                                                     | Fast-path routing, LLM gate contract, enforcement resolver                                  |
| [Reasoner abstraction](reasoners/index.md)                                  | Framework abstraction for swappable reasoners                                               |
| [Reasoner handoff](reasoners/reasoner-handoff.md)                           | The `build_observation()` contract — read before touching features, scorer, or gate         |
| [Account takeover reasoner](reasoners/account-takeover/account-takeover.md) | Reference reasoner: events, features, scorer, calibration                                   |

## Diagrams

D2 sources live under
[`docs/design/diagrams/`](https://github.com/charleskelley/decision-ledger/tree/main/docs/design/diagrams);
rendered SVGs are committed under `docs/assets/diagrams/`. Regenerate
with `just diagrams` (requires `brew install d2`).

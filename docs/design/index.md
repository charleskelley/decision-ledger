# Design

DecisionLedger is a decision system of record with a gating service. The
**framework** (`core/`, `app/`, `eval/`) validates each decision, resolves
its subject, runs its gates, enforces deterministically, and writes the
Decision Bundle; its only infrastructure is Postgres. Three kinds of plugin
work with it: **reasoners**, which decide or propose an action about a
subject and submit it, in process or over HTTP; **gates**, which the ledger
runs on a decision; and **identity resolvers**, which the enterprise
supplies to say who each decision's subject is. The framework imports none
of them. The reference reasoner is account takeover; the framework is
reasoner-agnostic.

## Where to start

- New to the project: read [Architecture](architecture.md) (C4 L1–L3
  with the directory layout and dependency rules), then
  [Pipeline](pipeline.md) for the decision order and the latency budget.
- Touching features, scorer, or a gate's context: read the
  [submission contract](reasoners/reasoner-handoff.md) first. Every
  reasoner-ledger interaction is one `Submission`, built by the reasoner's
  `build_submission()` and sent through a `LedgerClient`.
- Adding a new reasoner: read [Reasoner abstraction](reasoners/index.md),
  then the [new-reasoner checklist](reasoners/reasoner-handoff.md#new-reasoner-checklist)
  (over HTTP, or in process as a workspace member with a `ReasonerPlugin`
  and entry points) and DR-26 and DR-31.
- Adding a new gate: read [Gates](gates/index.md),
  [How to implement a gate](gates/implementation.md), and the
  [new-gate checklist](reasoners/reasoner-handoff.md#new-gate-checklist).
- Shipping an identity resolver, or reading a subject's timeline: read
  [Identity](identity.md).
- Working on retrieval, prompts, or eval thresholds: read
  [Evaluation](evaluation.md), the [RAG policy gate](gates/policy/index.md),
  and the relevant DRs in [Decisions](decisions.md).

## The map

| Document                                                                    | What it covers                                                                              |
|-----------------------------------------------------------------------------|---------------------------------------------------------------------------------------------|
| [Architecture](architecture.md)                                             | C4 L1–L3 diagrams, framework, member, and integration directory layout, dependency rules    |
| [Pipeline](pipeline.md)                                                     | Decision order (intake, identity, mandatory and advisory gates, combinator, write), fail-closed paths, latency budget |
| [Interfaces](core.md)                                                       | `core/` boundary, framework contracts, the ledger's HTTP surface                            |
| [Identity](identity.md)                                                     | The identity contract, the passthrough, the entity journal and its projection, timelines, shipping a resolver |
| [Infrastructure](infrastructure.md)                                         | Compose stack (framework Postgres + plugin `include:` files), `decisionledger.*` schema, env vars |
| [Gates](gates/index.md)                                                     | The gate contract, gate kinds, mandatory and advisory bindings, the combinator              |
| [How to implement a gate](gates/implementation.md)                          | Building a gate plugin against the universal contracts                                      |
| [RAG policy gate](gates/policy/index.md)                                    | The reference LLM gate: hybrid retrieval, prompts, verdicts, citations                      |
| [Policy corpus](gates/policy/policy-corpus.md)                              | Corpus documents, chunking strategy, jurisdiction model                                     |
| [Reasoner abstraction](reasoners/index.md)                                  | What a reasoner is, and what it provides to the ledger                                      |
| [Submission contract](reasoners/reasoner-handoff.md)                        | The wire `Submission`, record and gate, native subjects, the new-reasoner and new-gate checklists |
| [Account takeover reasoner](reasoners/account-takeover/account-takeover.md) | Reference reasoner: events, features, scorer, its bypass rules, calibration                 |
| [ATO data](reasoners/account-takeover/data.md)                              | ATO login-event schema, scorer output, Decision Bundle structure                            |
| [Scenarios](reasoners/account-takeover/scenarios.md)                        | Eight named ATO event patterns and what each exercises                                      |
| [Evaluation](evaluation.md)                                                 | The framework harness (consistency, robustness, shadow) and the RAG gate's harness (retrieval, faithfulness, citation), metrics, thresholds |
| [Decisions](decisions.md)                                                   | DR-1 through DR-32 — every load-bearing architecture decision and its rationale             |

## Diagrams

D2 sources live under
[`docs/design/diagrams/`](https://github.com/charleskelley/decision-ledger/tree/main/docs/design/diagrams);
rendered SVGs are committed under `docs/assets/diagrams/`. Regenerate
with `just diagrams` (requires `brew install d2`).

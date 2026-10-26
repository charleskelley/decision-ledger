# Code Reference

The reference is split across one page per framework package,
[`core`](core.md), [`app`](app.md), and [`eval`](eval.md), and one page per
workspace member: the reasoners
[`ato_reasoner`](reasoners/account-takeover.md) and
[`example_reasoner`](reasoners/example.md), the reference-pattern mocks
(DR-35) [`mock_stream_reasoner`](reasoners/mock-stream.md),
[`mock_batch_reasoner`](reasoners/mock-batch.md),
[`mock_text_reasoner`](reasoners/mock-text.md), and
[`mock_daily_reasoner`](reasoners/mock-daily.md), the gates
[`policy_rag_gate`](gates/policy-rag.md) and [`rules_gate`](gates/rules.md),
and the MCP integration [`decisionledger_mcp`](integrations/mcp.md).
[Identity](identity.md) is an index into the identity contract, which
renders on the `core` and `app` pages. Each package page renders the full
subtree of that package from the project's docstrings — use Material's
right-hand TOC and search to navigate.

This landing highlights the load-bearing types a reviewer should
read first to understand the control flow, the boundary contracts,
and the audit schema.

## The framework spine

The framework owns the ledger: intake, the decision's subject, the gate
set, deterministic enforcement, and audit-bundle persistence. Everything
below lives under `core/` and `app/`, which never import a reasoner, a gate,
or an integration (DR-24, DR-26 to DR-28).

- [`Submission`][core.submission.Submission] — The concrete wire
  contract every reasoner hands the ledger, to record a decision or to
  ask for a gate; the single data carrier across the boundary.
- [`DecisionBundle`][core.bundle.DecisionBundle] — Immutable audit
  record; the artifact replay reconstructs decisions from.
- [`DecisionAction`][core.actions.DecisionAction] — The terminal
  action enum (`ALLOW`, `CHALLENGE`, `HOLD`, `BLOCK`).
- [`submit`][app.submit.submit] — Framework half of the handoff:
  intake validation → the decision's subject (the identity step) → the
  decision's gate set (mandatory, then advisory) → enforcement and the
  combinator → the bundle write.
- [`ReasonerPlugin`][app.plugin.ReasonerPlugin] — What a reasoner
  publishes under the `decisionledger.reasoners` entry point (its
  registration and the assets its gates read);
  [`discover_plugins`][app.plugins.discover_plugins] loads them and
  [`create_app`][app.main.create_app] composes them into the ledger's HTTP
  surface ([`app.api`][app.api]).
- [`LocalClient`][app.client.LocalClient] and
  [`HttpClient`][app.client.HttpClient] — How a reasoner submits: in
  process or over HTTP, through the same operations
  ([`create_decision`][app.api.decisions.create_decision]).
- [`ReasonerRegistration`][core.observation.ReasonerRegistration] —
  A reasoner's gate set, bypass rules, recordable actions, native
  namespaces, and enforcement profile.
- [`ResolvedIdentity`][core.identity.ResolvedIdentity] — The decision's
  subject as the ledger records it (DR-32); see [Identity](identity.md).

## The gate contract

Gates are plugins the ledger runs on a decision (DR-28, DR-29); the
framework knows them only through these types.

- [`GatePlugin`][app.gates.GatePlugin] and [`Gate`][app.gates.Gate] —
  What a gate member publishes under the `decisionledger.gates` entry
  point, and what it builds.
- [`GateRequest`][core.gate_request.GateRequest] — What a gate receives:
  the submission, its own gate context, its budget, and the resolved
  subject.
- [`GateOutput`][core.gate.GateOutput] — The open output contract every
  gate returns: the universal verdict enforcement reads, plus the gate's
  opaque evidence snapshot and extra fields.
- [`most_restrictive`][core.gate.combinator.most_restrictive] — The
  combinator over ALLOW < CHALLENGE < HOLD < BLOCK.

## The reasoner contract

A reasoner owns a domain's events, features, scorer, and policy
mapping. The reference reasoner is account takeover; everything below
lives in the `ato_reasoner` package under `reasoners/account_takeover/src/`.

- [`build_submission`][ato_reasoner.assembler.build_submission]
  — Reasoner half of the handoff. Translates the domain's
  `ScorerOutput` into a `ReasonerContext` and the policy gate's
  `GateContext`, and records the login under a registered score band
  (`allow-band`, `block-band`) or asks for the gate.
- [`run_ato_decision`][ato_reasoner.pipeline.run_ato_decision]
  — Domain pipeline orchestrator: features → scorer → assembler →
  framework.
- [`ResolutionAttempt`][core.resolution.ResolutionAttempt] —
  Append-only resolver record for non-terminal actions
  (`CHALLENGE`, `HOLD`).

## Evaluation

Evaluation is split by owner (DR-28): the framework harness runs the
consistency, robustness, and shadow dimensions through each reasoner's
pipeline, and the RAG policy gate's own harness runs retrieval,
faithfulness, and citation. Both produce the same report schema; failures
block promotion.

- [`run_eval`][eval.runners.harness.run_eval] — Harness entry point.
  Invoked by `make eval-<reasoner>` through the CLI (`--reasoner-id`), and
  reused by the gate's harness (`make eval-gate-policy_rag`).
- [`EvalAdapter`][eval.adapter.EvalAdapter] — What a reasoner publishes
  under the `decisionledger.eval_adapters` entry point: event type,
  golden datasets, and pipeline driver.
- [`EvalReport`][core.eval.EvalReport] — Aggregated report across the
  dimensions that ran; the schema of the committed baselines under
  `outputs/eval/`.
- [`EvalDimension`][core.eval.EvalDimension] — The dimension enum
  (`RETRIEVAL`, `FAITHFULNESS`, `CONSISTENCY`, `CITATION`, `ROBUSTNESS`,
  `SHADOW`).

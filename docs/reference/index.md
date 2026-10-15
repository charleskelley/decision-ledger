# Code Reference

The reference is split across one page per top-level package:
[`core`](core.md), [`app`](app.md), [`eval`](eval.md), plus one page
per reasoner workspace member — today
[`ato_reasoner`](reasoners/account-takeover.md), which covers the ATO
package in full, including its generator and eval subpackages.
Each renders the full subtree of that package from the project's
docstrings — use Material's right-hand TOC and search to navigate.

This landing highlights the load-bearing types a reviewer should
read first to understand the control flow, the boundary contracts,
and the audit schema.

## The framework spine

The framework owns runtime orchestration, deterministic enforcement,
and audit-bundle persistence. Everything below lives under `core/`
and `app/` — never imports from a reasoner package (DR-24, DR-26).

- [`Submission`][core.submission.Submission] — The concrete wire
  contract every reasoner hands the ledger, to record a decision or to
  ask for a gate; the single data carrier across the boundary.
- [`DecisionBundle`][core.bundle.DecisionBundle] — Immutable audit
  record; the artifact replay reconstructs decisions from.
- [`DecisionAction`][core.actions.DecisionAction] — The terminal
  action enum (`ALLOW`, `CHALLENGE`, `HOLD`, `BLOCK`).
- [`submit`][app.submit.submit] — Framework half of the handoff:
  intake validation → the decision's gate set (mandatory, then advisory)
  → enforcement and the combinator → audit.
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
  A reasoner's identity, gate set, bypass rules, and enforcement profile.

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
- [`GateOutput`][core.gate.GateOutput] — The open output contract every
  gate returns (DR-28): the universal verdict enforcement reads, plus the
  gate's opaque evidence snapshot and extra fields.
- [`ResolutionAttempt`][core.resolution.ResolutionAttempt] —
  Append-only resolver record for non-terminal actions
  (`CHALLENGE`, `HOLD`).

## Evaluation

The 5D harness governs release candidates. Five dimensions, each with
its own metric schema; failures block promotion.

- [`run_eval`][eval.runners.harness.run_eval] — Harness entry point.
  Invoked by `make eval` / `make eval-<reasoner>` through the CLI
  (`--reasoner-id`).
- [`EvalAdapter`][eval.adapter.EvalAdapter] — What a reasoner publishes
  under the `decisionledger.eval_adapters` entry point: event type,
  golden datasets, and pipeline driver.
- [`EvalReport`][core.eval.EvalReport] — Aggregated report across the
  five dimensions; the `outputs/eval/eval-report-v1.json` schema.
- [`EvalDimension`][core.eval.EvalDimension] — The dimension enum
  (`RETRIEVAL`, `FAITHFULNESS`, `CONSISTENCY`, `CITATION`,
  `ROBUSTNESS`).

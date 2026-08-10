# Code Reference

The reference is split across one page per top-level package:
[`core`](core.md), [`app`](app.md), [`reasoner`](reasoner.md),
[`eval`](eval.md), [`generator`](generator.md), [`tools`](tools.md).
Each renders the full subtree of that package from the project's
docstrings — use Material's right-hand TOC and search to navigate.

This landing highlights the load-bearing types a reviewer should
read first to understand the control flow, the boundary contracts,
and the audit schema.

## The framework spine

The framework owns runtime orchestration, deterministic enforcement,
and audit-bundle persistence. Everything below lives under `core/`
and `app/` — never imports from `reasoner.*` (DR-24).

- [`Observation`][core.observation.Observation] — Protocol every
  reasoner satisfies; the single data carrier across the boundary.
- [`DecisionBundle`][core.bundle.DecisionBundle] — Immutable audit
  record; the artifact replay reconstructs decisions from.
- [`DecisionAction`][core.actions.DecisionAction] — The terminal
  action enum (`ALLOW`, `CHALLENGE`, `HOLD`, `BLOCK`).
- [`execute_decision`][app.decide.execute_decision] — Framework half
  of the handoff: retrieval → gate → enforcement → audit.

## The reasoner contract

A reasoner owns a domain's events, features, scorer, and policy
mapping. The reference reasoner is account takeover; everything below
lives under `reasoner/account_takeover/`.

- [`build_observation`][reasoner.account_takeover.assembler.build_observation]
  — Reasoner half of the handoff. Translates the domain's
  `ScorerOutput` into `ReasonerContext` + `GateContext` + (optionally)
  `FastPathRecord`.
- [`run_ato_decision`][reasoner.account_takeover.pipeline.run_ato_decision]
  — Domain pipeline orchestrator: features → scorer → assembler →
  framework.
- [`PolicyGateOutput`][core.gate.policy.PolicyGateOutput] — The
  structured output the LLM gate must produce; verified against schema
  before reaching enforcement.
- [`ResolutionAttempt`][core.resolution.ResolutionAttempt] —
  Append-only resolver record for non-terminal actions
  (`CHALLENGE`, `HOLD`).

## Evaluation

The 5D harness governs release candidates. Five dimensions, each with
its own metric schema; failures block promotion.

- [`run_eval`][eval.runners.harness.run_eval] — Harness entry point.
  Invoked by `make eval`.
- [`EvalReport`][core.eval.EvalReport] — Aggregated report across the
  five dimensions; the `outputs/eval/eval-report-v1.json` schema.
- [`EvalDimension`][core.eval.EvalDimension] — The dimension enum
  (`RETRIEVAL`, `FAITHFULNESS`, `CONSISTENCY`, `CITATION`,
  `ROBUSTNESS`).

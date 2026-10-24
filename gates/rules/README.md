# decisionledger-gate-rules

The zero-infrastructure rules gate for DecisionLedger, packaged as a uv
workspace member. Import package: `rules_gate`; distribution:
`decisionledger-gate-rules`; `gate_id`: `rules`; kind: `rules`.

It evaluates deployment-registered rule sets of declarative predicates over
a reasoner's evidence (`reasoner_context.label_value` and
`reasoner_context.feature_set`), using the framework's closed predicate
language (`core.gate.predicate`: `Compare`, `AllOf`, `AnyOf`, `Not`, written
as `kind: compare | all | any | not`), the same language registered bypass
rules use. It is deterministic (`deterministic=True`), so enforcement skips
the adversarial-probe and low-confidence tiers for it, and its evidence
carries no jurisdictions, so the jurisdiction-conflict tier is skipped too.
The framework (`decision-ledger`) never imports or depends on it.

See [How to implement a gate](../../docs/design/gates/implementation.md) for
the contract (DR-28 in `docs/design/decisions.md`).

## How it plugs in

One entry point in `pyproject.toml`, named after the `gate_id`:

| Group | Target | Consumed by |
| --- | --- | --- |
| `decisionledger.gates` | `rules_gate.plugin:PLUGIN` | `app.gates.discover_gates` (runtime: identity, `config_model`, `build`) |

The root workspace installs it through the `gates` dependency group (a
default group), so a bare `uv sync` at the repo root is enough. Entry points
refresh only on reinstall: re-run `uv sync` after editing this
`pyproject.toml`.

Infrastructure: none of its own. No compose file, no schema, no index, no
`gate.mk`, and no connection: `build` takes only the framework store's
evidence escrow (`framework.store.artifacts`), through which the gate keeps a
copy of every rule set it applies in the framework's
`decisionledger.evidence_artifacts` (DR-34). Built without a framework, it
escrows nothing.

## Rule sets

Rule sets are YAML files (`*.yaml` or `*.yml`, one rule set per file) in the
directory named by `RULES_GATE_RULESETS_DIR`, loaded once when the gate is
built. A directory that does not exist, a malformed file, or two files with
one `ruleset_id` fails the build, so the deployment refuses to start. A blank
setting (the default) registers no rule sets, and every decision addressed to
the gate then fails closed with `unknown_ruleset`.

Each rule set belongs to one reasoner (`reasoner_id`). Rules are evaluated in
order; rule ids are unique within a set, and a set has at least one rule:

```yaml
ruleset_id: widget-guardrails
version: "1.0.0"
reasoner_id: example-reasoner
description: Hold thin-history widgets; block extreme scores.
default_action: ALLOW
rules:
  - rule_id: thin-history-hold
    when: {kind: compare, field: feature_set.thin_history, op: eq, value: true}
    action: HOLD
    required_controls: [calibration_review]
  - rule_id: extreme-score-block
    when: {kind: compare, field: label_value, op: gt, value: 0.9}
    action: BLOCK
```

The example is illustrative: ATO and the example reasoner do not bind the
rules gate; the batch mock (`reasoners/mock_batch`, DR-35) does, under its
rule set `rulesets/mock-batch-v1.yaml`. A reasoner that does binds it in its
registration (`GateBinding(gate_id="rules")`) and addresses it with a gate
context naming a registered rule set:

```python
GateContext(gate_id="rules", gate_config={"ruleset_id": "widget-guardrails"})
```

The framework validates `gate_config` against `RulesGateConfig` (exactly one
key, `ruleset_id`) at intake. A reasoner selects a rule set; it never submits
rules, so the rules a decision is held to are the deployment's. Because the
gate needs a `ruleset_id`, it cannot be mandated
(`DECISIONLEDGER_MANDATORY_GATES`) for a reasoner that does not bind it:
startup refuses a mandated gate that rejects the empty configuration.

## The output

- **Verdict.** It permits the actions of every matching rule (enforcement
  takes the most conservative), or the rule set's `default_action` when none
  matches, with the matching rules' `required_controls` and confidence 1.0.
- **Evidence.** One reference per matching rule (`source_id`
  `<ruleset_id>@<version>`, `locator` the rule id), or one with `locator`
  `default_action` when none matched. Every reference carries the SHA-256 of
  the applied rule set's canonical JSON (`content_hash`: sorted keys, no
  spaces, UTF-8, of the rule set as the gate applied it, recorded in the
  snapshot as `ruleset_hash`; it is not always reproducible from the stored
  `ruleset` JSON, whose numbers the store may render differently, but the
  escrowed artifact holds the exact bytes) and the digest the escrow
  returned for that JSON (`artifact_hash`; none with escrow off).
- **Snapshot.** `evidence_snapshot` is a `RulesGateSnapshot`: the rule set as
  evaluated, the matching rule ids, whether the default applied, the
  observed value of every field the rules reference, so the bundle is
  auditable without the rule files, and the digest every reference names
  (`ruleset_hash`, computed when the gate decided).
- **No verdict.** An unknown rule set (`unknown_ruleset`), a rule set
  registered for another reasoner (`ruleset_reasoner_mismatch`; its rules are
  not copied into this tenant's bundle), or a predicate that cannot evaluate,
  such as a feature the submission does not carry (`predicate_error`),
  returns `verdict=None` with `extra = {"failure": ..., "detail": ...}`.
  Enforcement holds the decision.

## Escrow and verify

When a decision is verified (`POST /api/v1/decisions/{decision_id}/verify`),
the gate (an `EvidenceVerifier`) says how each of its references stands. A
reference must carry the digest the output's snapshot recorded
(`ruleset_hash`; it is not recomputed from the stored rule set, whose
numbers the store may render differently), else it is `not_verifiable`.
Without `?live=true`, the escrowed copy is read back: `intact` when it is
that rule set, `not_verifiable` when nothing was escrowed or the escrow no
longer holds it. With `?live=true`, the rule set's file (`load_rulesets`
returns each rule set with the file it came from) is read again and
canonicalized: `intact` when it holds the same rules (a reformatted or
commented file stays `intact`), `source_changed` when it holds other rules,
`source_unavailable` when it cannot be read, parsed, or canonicalized as a
rule set, or the gate does not know its file. An output with no verdict
cites nothing and escrows nothing.

## Tests

`tests/` (no `__init__.py`) runs under the root `pytest` with no Docker.

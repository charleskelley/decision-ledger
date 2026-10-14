# decisionledger-gate-rules

The zero-infrastructure rules gate for DecisionLedger, packaged as a uv
workspace member. Import package: `rules_gate`; distribution:
`decisionledger-gate-rules`; `gate_id`: `rules`.

It evaluates deployment-registered rule sets of declarative predicates over
a reasoner's evidence (`reasoner_context.label_value` and
`reasoner_context.feature_set`), using the framework's closed predicate
language (`core.gate.predicate`: `Compare`, `AllOf`, `AnyOf`, `Not`). It is
deterministic (`deterministic=True`), so enforcement skips the
adversarial-probe and low-confidence tiers for it, and its evidence carries
no jurisdictions, so the jurisdiction-conflict tier is skipped too. The
framework (`decision-ledger`) never imports or depends on it.

See DR-28 in `docs/design/decisions.md` for the gate contract.

## How it plugs in

One entry point in `pyproject.toml`, named after the `gate_id`:

| Group | Target | Consumed by |
| --- | --- | --- |
| `decisionledger.gates` | `rules_gate.plugin:PLUGIN` | `app.gates.discover_gates` (runtime: identity, `config_model`, `build`) |

The root workspace installs it through the `gates` dependency group (a
default group), so a bare `uv sync` at the repo root is enough. Entry points
refresh only on reinstall: re-run `uv sync` after editing this
`pyproject.toml`.

Infrastructure: none. No compose file, no database, no index.

## Rule sets

Rule sets are YAML files in the directory named by `RULES_GATE_RULESETS_DIR`
(one rule set per file). Each belongs to one reasoner (`reasoner_id`), and
the gate refuses — fails closed with no verdict, which enforcement holds —
when another reasoner selects it:

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

A reasoner routes a decision to the gate with a gate context naming a
registered rule set:

```python
GateContext(gate_id="rules", gate_config={"ruleset_id": "widget-guardrails"})
```

The framework validates `gate_config` against `RulesGateConfig` at intake.
The verdict permits the actions of every matching rule (enforcement takes
the most conservative) or the rule set's `default_action` when none matches.
The bundle's `gate_output.evidence_snapshot` records the rule set as
evaluated, the matching rule ids, and the observed values of every field the
rules reference.

## Tests

`tests/` (no `__init__.py`) runs under the root `pytest` with no Docker.

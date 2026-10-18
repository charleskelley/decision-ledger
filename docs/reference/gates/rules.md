# `rules_gate`

The rules gate, shipped as the `decisionledger-gate-rules` uv workspace
member under `gates/rules/` (import package `rules_gate`, `gate_id`
`rules`). It is the zero-infrastructure reference gate: rule sets registered
by the deployment (YAML files under `RULES_GATE_RULESETS_DIR`, each owned by
one `reasoner_id`) of predicates over a submission's `label_value` and
`feature_set`, written in the framework's closed predicate language
([`core.gate.predicate`][core.gate.predicate]) and evaluated
deterministically. A reasoner selects a rule set with
`gate_config = {"ruleset_id": ...}` and never authors rules. It plugs in
through the `decisionledger.gates` entry point (`rules_gate.plugin:PLUGIN`);
the framework never imports it.

See [Gates](../../design/gates/index.md) for the design.

::: rules_gate
    options:
      show_root_heading: false

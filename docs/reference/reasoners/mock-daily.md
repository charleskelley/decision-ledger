# `mock_daily_reasoner`

The daily mock, shipped as the `mock-daily-reasoner` uv workspace member
under `reasoners/mock_daily/` (import package `mock_daily_reasoner`,
`reasoner_id` `mock-daily-reasoner`): the deterministic computation with
exceptions, one of the
[reference reasoner patterns](../../design/reasoners/patterns.md) (DR-35). A
mock that demonstrates the framework's contracts, not a product: a
deterministic synthetic day of account settlements;
[`compute_drift`][mock_daily_reasoner.compute.compute_drift], pure
arithmetic over each account's target and actual amounts; the
[`build_submission`][mock_daily_reasoner.assembler.build_submission]
assembler, which records an account within tolerance under its one bypass
rule (`within-tolerance`) and leaves every other account, an exception, to
the rules gate, bound mandatory with an empty binding configuration, so the
gate runs on every decision, recorded ones too;
[`run_day`][mock_daily_reasoner.daily.run_day], which submits a day one
decision per request and reads each decision's bundle back; a CLI; and an
eval adapter whose driver composes the real rules gate over the member's
rule set.

It reaches the framework only through its `PLUGIN` (entry point group
`decisionledger.reasoners`), its eval `ADAPTER`
(`decisionledger.eval_adapters`), and the `Submission`s its CLI sends over
HTTP. The ledger never imports it: it registers the mock by the plugin, by
the generated `registration.yaml`, or by `PUT
/api/v1/reasoners/mock-daily-reasoner` with the plugin excluded, and,
whichever the source, refuses the registration unless it composes the
rules gate. No case needs an asset root (the plugin's prompt and corpus
directories are empty); the rule set every submission names,
`rulesets/mock-daily-v1.yaml` at the member root, is deployment
configuration the rules gate reads from `RULES_GATE_RULESETS_DIR`.

::: mock_daily_reasoner
    options:
      show_root_heading: false

# `mock_batch_reasoner`

The batch mock, shipped as the `mock-batch-reasoner` uv workspace member
under `reasoners/mock_batch/` (import package `mock_batch_reasoner`,
`reasoner_id` `mock-batch-reasoner`): the daily batch scorer, one of the
[reference reasoner patterns](../../design/reasoners/patterns.md) (DR-35). A
mock that demonstrates the framework's contracts, not a product: a
deterministic synthetic day of account positions; a transparent scorer; the
[`build_submission`][mock_batch_reasoner.assembler.build_submission]
assembler, which records a row under one of four disjoint bypass rules
(`current-account`, `watch-list`, `delinquent-hold`, `charge-off`) and
leaves a flagged row, which no rule covers, to the rules gate (advisory,
with an empty binding configuration);
[`run_day`][mock_batch_reasoner.batch.run_day], which records a day through
the ledger's batch endpoint in chunks of at most 10,000 lines, provokes one
`BATCH_RECORD_ONLY` rejection, and submits the exceptions in gate mode; a
CLI; and an eval adapter whose driver composes the real rules gate over the
member's rule set.

It reaches the framework only through its `PLUGIN` (entry point group
`decisionledger.reasoners`), its eval `ADAPTER`
(`decisionledger.eval_adapters`), and the `Submission`s its CLI sends over
HTTP. The ledger never imports it: it registers the mock by the plugin, by
the generated `registration.yaml`, or by `PUT
/api/v1/reasoners/mock-batch-reasoner` with the plugin excluded. No case
needs an asset root (the plugin's prompt and corpus directories are empty);
the rule set every submission names, `rulesets/mock-batch-v1.yaml` at the
member root, is deployment configuration the rules gate reads from
`RULES_GATE_RULESETS_DIR`.

::: mock_batch_reasoner
    options:
      show_root_heading: false

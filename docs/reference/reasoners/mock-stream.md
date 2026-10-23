# `mock_stream_reasoner`

The stream mock, shipped as the `mock-stream-reasoner` uv workspace member
under `reasoners/mock_stream/` (import package `mock_stream_reasoner`,
`reasoner_id` `mock-stream-reasoner`): the real-time event scorer, one of the
[reference reasoner patterns](../../design/reasoners/patterns.md) (DR-35). A
mock that demonstrates the framework's contracts, not a product: an async
in-memory stream of transfers with dedup, a lateness bound, and dead
letters; per-account sliding windows on event time; a transparent scorer;
the [`build_submission`][mock_stream_reasoner.assembler.build_submission]
assembler, which records the clear bands under two bypass rules
(`steady-activity`, `activity-surge`) and asks the ledger to gate the
middle through the RAG policy gate (advisory, bound to its own corpus at
tier 2, with Tier 3 on fresh accounts); its own service and CLI; and an eval
adapter with a scripted policy gate double.

It reaches the framework only through its `PLUGIN` (entry point group
`decisionledger.reasoners`), its eval `ADAPTER`
(`decisionledger.eval_adapters`), and the `Submission`s its service sends
over HTTP. The ledger never imports it: it registers the mock by the plugin,
by the generated `registration.yaml`, or by `PUT
/api/v1/reasoners/mock-stream-reasoner` with the plugin excluded, and in the
last two cases reads the mock's prompts and corpus from the RAG policy
gate's asset root.

::: mock_stream_reasoner
    options:
      show_root_heading: false

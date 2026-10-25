# `mock_text_reasoner`

The text mock, shipped as the `mock-text-reasoner` uv workspace member
under `reasoners/mock_text/` (import package `mock_text_reasoner`,
`reasoner_id` `mock-text-reasoner`): the LLM-native classifier, one of the
[reference reasoner patterns](../../design/reasoners/patterns.md) (DR-35). A
mock that demonstrates the framework's contracts, not a product: customer
messages to an imaginary bank's automated assistant;
[`classify`][mock_text_reasoner.classifier.classify], a deterministic
keyword classifier that gives each message a categorical label; the
[`build_submission`][mock_text_reasoner.assembler.build_submission]
assembler, which makes every message a gate-mode submission proposing its
label's action as a floor, for the ledger to gate through the RAG policy
gate (advisory), bound to two corpora by authority tier, regulatory text at
tier 1 and practice notes at tier 2, which disagree on one category so the
gate's protocol hold is exercised;
[`decide_message`][mock_text_reasoner.pipeline.decide_message], which
labels, assembles, and submits one message; a CLI; and an eval adapter
with a scripted policy gate double.

It reaches the framework only through its `PLUGIN` (entry point group
`decisionledger.reasoners`), its eval `ADAPTER`
(`decisionledger.eval_adapters`), and the `Submission`s its CLI sends over
HTTP. The ledger never imports it: it registers the mock by the plugin, by
the generated `registration.yaml`, or by `PUT
/api/v1/reasoners/mock-text-reasoner` with the plugin excluded, and in the
last two cases reads the mock's prompts and corpora from the RAG policy
gate's asset root. Each corpus is loaded from its own directory
(`corpus/regulatory/`, `corpus/practice/`) by its own corpus loader call.

::: mock_text_reasoner
    options:
      show_root_heading: false

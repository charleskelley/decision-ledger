# `example_reasoner`

The documented example reasoner, shipped as the `example-reasoner` uv
workspace member under `reasoners/example/` (import package
`example_reasoner`, `reasoner_id` `example-reasoner`). Its domain is
deliberately imaginary (widgets inspected in two regions, scored by a
transparent heuristic), so every part of the reasoner contract stays short
enough to read: its own event type, a pure scorer, a registration with one
advisory gate (the RAG policy gate) and two bypass rules (`low-score`,
`high-score`), the
[`build_submission`][example_reasoner.assembler.build_submission] assembler,
a pipeline that submits through any `LedgerClient`, a prompt and a
three-document policy corpus, an eval adapter with its datasets, and a CLI.

It reaches the framework only through its `PLUGIN` (entry point group
`decisionledger.reasoners`), its eval `ADAPTER`
(`decisionledger.eval_adapters`), and the `Submission`s it sends. It names no
native subject, so the passthrough records each decision under the
`entity_id` it sends. `tests/_stub_reasoner` stays the framework's minimal
test double; this package is the documentation.

::: example_reasoner
    options:
      show_root_heading: false

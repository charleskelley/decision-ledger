# `ato_reasoner`

The account-takeover reference reasoner, shipped as the `ato-reasoner`
uv workspace member under `reasoners/account_takeover/` (import package
`ato_reasoner`). It owns the domain's events, features, scorer, prompts,
policy corpus, generator, and eval adapter. It reaches the framework only
through its `PLUGIN` ([`AtoPlugin`][ato_reasoner.plugin.AtoPlugin]) (entry point group
`decisionledger.reasoners`), the
[`build_submission`][ato_reasoner.assembler.build_submission]
handoff, and its [`ReasonerRegistration`][core.observation.ReasonerRegistration]
record.

Adding a second reasoner means adding a sibling workspace member with the
same shape and its own entry points — no framework changes required.

::: ato_reasoner
    options:
      show_root_heading: false

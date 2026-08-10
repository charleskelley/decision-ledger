# `reasoner`

Domain reasoners. Each subpackage owns a domain's events, features,
scorer, and policy mapping; the only export to the framework is the
[`build_observation`][reasoner.account_takeover.assembler.build_observation]
handoff and a [`RegisteredReasoner`][core.observation.ReasonerRegistration]
record.

The reference reasoner is account takeover. Adding a second reasoner
means creating a sibling subpackage with the same shape — no
framework changes required.

::: reasoner
    options:
      show_root_heading: false

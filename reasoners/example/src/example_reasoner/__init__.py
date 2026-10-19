"""The example reasoner — the reasoner contract end to end, on widgets.

Promoted from the framework's test-only stub reasoner (``tests/_stub_reasoner``
stays minimal for framework tests; this package is the documentation). The
domain is deliberately imaginary — widgets inspected in two regions, scored
by a transparent heuristic — so nothing here can be mistaken for production
behavior, and every part of the contract is short enough to read:

Submodules:
    events:    ``WidgetEvent`` — the reasoner's own event; it names the widget
               as its native subject, ``(example-widget, widget_id)``, and
               derives ``entity_id`` under that declared namespace.
    scorer:    ``score_widget`` — a pure, documented heuristic (no model file).
    registry:  ``EXAMPLE_REGISTRATION`` — one advisory gate (the RAG policy
               gate), two bypass rules (``low-score``, ``high-score``), and
               the widget's native namespace.
    assembler: ``build_submission`` — records a widget under a bypass rule,
               or asks the ledger to gate it, addressing the policy gate.
    pipeline:  ``decide_widget`` — score, assemble, submit through a
               ``LedgerClient`` (in process or over HTTP).
    plugin:    ``PLUGIN`` — the registration and the policy gate's assets
               (``prompts/``, the three-document ``corpus/``).
    eval:      The ``EvalAdapter``, its driver, and its datasets.
    __main__:  A CLI that prints sample submissions and NDJSON batches and
               submits widgets to a ledger.
"""

"""The stub reasoner — a second reasoner that proves "zero framework diff".

This package is the smallest thing that satisfies every contract the framework
asks of a reasoner: a ``ReasonerRegistration`` (``STUB_REGISTRATION``: a gate
set and two bypass rules), an assembler that turns its own event
(``StubEvent``) into a ``Submission``, a prompt template directory, a policy
corpus directory, and a ``ReasonerPlugin`` (``PLUGIN``). It stays minimal for
framework tests; ``reasoners/example`` is the documented reference reasoner
promoted from it.
Framework tests compose it exactly the way a deployment composes a real
reasoner — through ``create_app(plugins=[...])`` or an
``importlib.metadata.EntryPoint`` under ``decisionledger.reasoners`` — so a
framework test never has to import the ATO reasoner.

The domain is deliberately silly (widgets with a ``widget_score``) so that
nothing here can be mistaken for production behavior.
"""

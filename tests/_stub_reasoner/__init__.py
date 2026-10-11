"""The stub reasoner — a second reasoner that proves "zero framework diff".

This package is the smallest thing that satisfies every contract the framework
asks of a reasoner: an ``Observation`` type (``StubEvent``), a
``ReasonerRegistration`` (``STUB_REGISTRATION``), a FastAPI router, a prompt
template directory, a policy corpus directory, and a ``ReasonerPlugin``
(``PLUGIN``). Framework tests compose it exactly the way a deployment composes
a real reasoner — through ``create_app(plugins=[...])`` or an
``importlib.metadata.EntryPoint`` under ``decisionledger.reasoners`` — so a
framework test never has to import the ATO reasoner.

The domain is deliberately silly (widgets with a ``widget_score``) so that
nothing here can be mistaken for production behavior.
"""

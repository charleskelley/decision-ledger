"""The fake gate — a test-only gate that proves "zero framework diff" for gates.

The gate-side twin of ``tests/_stub_reasoner``: the smallest thing that
satisfies every contract the framework asks of a gate — a ``GatePlugin``
(``PLUGIN``, and a deterministic variant ``DETERMINISTIC_PLUGIN``) whose
``build`` returns a ``Gate`` that answers ``evaluate(GateRequest)`` with the
universal ``GateOutput``. Its behavior is scripted by ``gate_config``
(``FakeGateConfig``): the actions it permits, its confidence and controls,
the jurisdictions its evidence carries, a schema failure, or a raised error.

Framework tests compose it exactly the way a deployment composes a real gate
— ``create_app(gate_plugins=[...])``, ``build_framework_services(...,
gate_plugins=[...])``, or an ``importlib.metadata.EntryPoint`` under
``decisionledger.gates`` — so no framework test needs the policy gate's
Elasticsearch, models, or LLM, nor the rules gate member.
"""

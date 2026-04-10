"""Core framework contracts for DecisionLedger.

Pure Python, zero infrastructure dependencies.

Package structure:

Top-level modules (framework output artifacts and vocabulary):
    actions.py:    DecisionAction — the framework's final action vocabulary.
    bundle.py:     DecisionBundle and all bundle-composition types.
    exceptions.py: Framework exception types.

Subpackages (self-contained functional concerns):
    observation/   Everything a domain reasoner must supply as the intake
                   contract: Observation protocol, GateContext, ReasonerContext,
                   registration contracts, and the intake validator.
    gate/          Everything needed to interact with any gate type: GateRouting,
                   gate output schema, prompt registry contracts, corpus metadata.
    eval/          Evaluation dimension contracts and metric interfaces.

Dependency rule: subpackages never import from top-level bundle.py.
Top-level modules reach into subpackages via their __init__.py interfaces.
No subpackage imports from another subpackage directly.

If your code cannot be unit-tested without Docker, it does not belong here.
Only standard library, pydantic, and pure-Python utilities are permitted imports.
"""

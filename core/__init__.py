"""Core framework contracts for DecisionLedger.

Pure Python, zero infrastructure dependencies.

Package structure:

Top-level modules (framework output artifacts and vocabulary):
    actions.py:    DecisionAction — the framework's decision-time action vocabulary.
    routes.py:     GateRoute — gate path enum produced by the reasoner,
                   consumed by the gate. Lives at the root because both
                   ``observation/`` and ``gate/`` need it; placing it in
                   either subpackage forces a cross-subpackage import.
    bundle.py:     DecisionBundle and bundle-composition types.
    resolution.py: ResolutionAttempt — append-only resolution log surface for
                   non-terminal decisions (CHALLENGE, HOLD).
    exceptions.py: Framework exception types.

Subpackages (self-contained functional concerns):
    observation/   Everything a domain reasoner must supply as the intake
                   contract: Observation protocol, GateContext, ReasonerContext,
                   registration contracts, and the intake validator.
    gate/          Universal gate contracts (GateInput, GateOutput, GateVerdict)
                   plus per-gate-kind subpackages (e.g., ``gate/policy/`` for
                   the LLM-backed policy gate's concrete subclasses).
    eval/          Evaluation dimension contracts and metric interfaces.

Dependency rule: subpackages never import from top-level bundle.py.
Top-level modules reach into subpackages via their __init__.py interfaces.
No subpackage imports from another subpackage directly.

Action vocabulary (see DR-18 for the full rationale):
    * ``decision_action``    — what the pipeline produced (on DecisionBundle).
    * ``resolution_action``  — what a resolver produced (on ResolutionAttempt).
    * ``realized_action``    — derived: terminal decision_action if present,
                                else the first terminal resolution_action in
                                the attempt chain. Never stored.

If your code cannot be unit-tested without Docker, it does not belong here.
Only standard library, pydantic, and pure-Python utilities are permitted imports.
"""

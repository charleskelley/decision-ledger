"""Core framework contracts for DecisionLedger.

Pure Python, zero infrastructure dependencies.

Package structure:

Top-level modules (framework output artifacts and vocabulary):
    actions.py:    DecisionAction — the framework's decision-time action vocabulary.
    submission.py: Submission — the concrete wire contract a reasoner hands the
                   ledger to record or to gate a decision (DR-30, DR-31), with
                   its bypass citation, the reserved NativeSubject, and the
                   pure intake and bypass-rule checks. Lives at the root
                   because it composes ``observation/`` types and is carried
                   into the gate layer.
    bundle.py:     DecisionBundle (schema version 2) and GateRecord.
    gate_request.py: GateRequest — what the framework hands a gate. Lives at
                   the root because it carries the Submission into the gate
                   layer, and ``gate/`` never imports ``observation/``.
    enforcement.py: EnforcementDecision — enforcement's typed output.
    exceptions.py: Framework exception types.

Subpackages (self-contained functional concerns):
    observation/   What a domain reasoner supplies and what the ledger knows
                   about it: ReasonerContext, GateContext, and the
                   registration contracts (GateBinding, BypassRule,
                   ReasonerRegistration).
    gate/          Universal gate contracts (GateInput, the open GateOutput and
                   EvidenceRef, GateVerdict), the predicate language, and the
                   conjunction combinator. A gate's own typed models live in
                   that gate's workspace member, never here (DR-28).
    resolution/    ResolutionAttempt subclasses — the append-only resolution
                   log for non-terminal decisions (CHALLENGE, HOLD).
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

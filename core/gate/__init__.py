"""Gate layer — universal contracts for any gate in DecisionLedger.

Defines what every gate satisfies and what the framework stores (DR-28):

- ``GateOutput`` — the open output contract: a typed universal base
  (``gate_id``, ``deterministic``, ``verdict``, ``evidence``) plus an opaque,
  gate-owned extension (``evidence_snapshot``, ``extra``) the framework
  stores verbatim and never interprets. The framework never defines or
  imports a ``GateOutput`` subclass.
- ``EvidenceRef`` — a reference to one source a gate relied on.
- ``GateVerdict`` — the universal verdict enforcement consumes.
- ``GateInput`` — the framework's record that it invoked a gate.
- The predicate language (``Compare``, ``AllOf``, ``AnyOf``, ``Not``,
  ``evaluate_predicate``) shared by the rules gate and registered bypass
  rules.
- The conjunction combinator (``most_restrictive``, ``SEVERITY_ORDER``,
  ``severity``) and ``GateStatus``, the state a gate ends a decision in
  (DR-29).

``GateRequest`` — what the framework hands a gate — lives at the framework
root in ``core.gate_request`` because it carries the reasoner's
``Submission`` (whose evidence types live in ``core/observation/``), and
``core/gate/`` does not import ``core/observation/`` (DR-11).

A gate's own typed models (a structured-output verdict schema, citations,
prompt templates, the schemas of its ``evidence_snapshot`` / ``extra``) live
in that gate's workspace member, never here: they are the gate's private
vocabulary, and the framework stores only the universal contracts above
(``tests/test_framework_boundary.py`` enforces it).

Public API at this level:
    GateInput:        The framework's record of one gate invocation.
    GateOutput:       Open output contract — universal base + opaque extension.
    EvidenceRef:      Reference to a source a gate relied on.
    GateVerdict:      Universal verdict — what enforcement consumes.
    Predicate:        Discriminated union of the four predicate node kinds.
    Compare, AllOf, AnyOf, Not:  Predicate nodes.
    evaluate_predicate, parse_predicate, referenced_fields:  Predicate helpers.
    most_restrictive, severity, SEVERITY_ORDER:  The conjunction combinator.
    GateStatus:       ``ran`` | ``skipped`` | ``failed``.
"""

from core.gate.combinator import (
    SEVERITY_ORDER,
    GateStatus,
    most_restrictive,
    severity,
)
from core.gate.input import GateInput
from core.gate.output import EvidenceRef, GateOutput
from core.gate.predicate import (
    AllOf,
    AnyOf,
    Compare,
    Not,
    Predicate,
    evaluate_predicate,
    parse_predicate,
    referenced_fields,
)
from core.gate.verdict import GateVerdict

__all__ = [
    "SEVERITY_ORDER",
    "AllOf",
    "AnyOf",
    "Compare",
    "EvidenceRef",
    "GateInput",
    "GateOutput",
    "GateStatus",
    "GateVerdict",
    "Not",
    "Predicate",
    "evaluate_predicate",
    "most_restrictive",
    "parse_predicate",
    "referenced_fields",
    "severity",
]

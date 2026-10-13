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

``GateRequest`` — what the framework hands a gate — lives at the framework
root in ``core.gate_request`` because it carries the intake ``Observation``,
and ``core/gate/`` does not import ``core/observation/`` (DR-11).

The ``core.gate.policy`` subpackage holds the reference policy gate's own
typed models (its structured-output verdict schema, citations, prompt
templates, corpus metadata, and the schemas of its ``evidence_snapshot`` /
``extra``). They are that gate's private vocabulary: this package does not
re-export them, and the framework's generic modules never import them
(``tests/test_framework_boundary.py`` enforces both).

Public API at this level:
    GateInput:        The framework's record of one gate invocation.
    GateOutput:       Open output contract — universal base + opaque extension.
    EvidenceRef:      Reference to a source a gate relied on.
    GateVerdict:      Universal verdict — what enforcement consumes.
    Predicate:        Discriminated union of the four predicate node kinds.
    Compare, AllOf, AnyOf, Not:  Predicate nodes.
    evaluate_predicate, parse_predicate, referenced_fields:  Predicate helpers.
"""

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
    "AllOf",
    "AnyOf",
    "Compare",
    "EvidenceRef",
    "GateInput",
    "GateOutput",
    "GateVerdict",
    "Not",
    "Predicate",
    "evaluate_predicate",
    "parse_predicate",
    "referenced_fields",
]

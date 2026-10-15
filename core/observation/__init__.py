"""Reasoner-supplied contracts — evidence, gate envelopes, and registration.

The building blocks of a ``core.submission.Submission`` that a domain
reasoner owns, plus what the ledger knows about the reasoner:

Public API:
    ReasonerContext:      Generic lattice for domain reasoner evidence.
    Contribution:         Per-event feature contribution from any method.
    LabelType:            Model output type enum (NUMERICAL, CATEGORICAL).
    AttributionSummary:   Flexible attribution evidence from the domain reasoner.
    GateContext:          Gate dispatch envelope (one per gate in a submission).
    GateBinding:          One gate in a reasoner's gate set (mandatory/advisory).
    BypassRule:           A registered condition under which a decision may be
                          recorded without the advisory gates.
    ReasonerRegistration: Framework declaration of a known domain reasoner.
    ReasonerRegistry:     Protocol for reasoner registries.
"""

from core.observation.gate_context import GateContext
from core.observation.reasoner_context import (
    AttributionSummary,
    Contribution,
    LabelType,
    ReasonerContext,
)
from core.observation.registration import (
    BypassRule,
    GateBinding,
    ReasonerRegistration,
    ReasonerRegistry,
)

__all__ = [
    "AttributionSummary",
    "BypassRule",
    "Contribution",
    "GateBinding",
    "GateContext",
    "LabelType",
    "ReasonerContext",
    "ReasonerRegistration",
    "ReasonerRegistry",
]

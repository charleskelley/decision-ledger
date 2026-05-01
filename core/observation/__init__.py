"""Observation layer — intake contract for domain reasoners.

Everything a domain reasoner must provide to cross the framework boundary:
the Observation protocol, GateContext, ReasonerContext, registration contracts,
and the intake validator.

Public API:
    Observation:               Protocol — minimum contract for any input event.
    validate_observation:      Framework intake validator — raises on violation.
    GateContext:               Gate dispatch envelope.
    Contribution:              Per-event feature contribution from any method.
    LabelType:                 Model output type enum (NUMERICAL, CATEGORICAL).
    AttributionSummary:        Flexible attribution evidence from the domain reasoner.
    ReasonerContext:           Generic lattice for domain reasoner evidence.
    ReasonerRegistration:      Framework declaration of a known domain reasoner.
    ReasonerRegistry:          Protocol for reasoner registries.
"""

from core.observation.gate_context import GateContext
from core.observation.observation import Observation, validate_observation
from core.observation.reasoner_context import (
    AttributionSummary,
    Contribution,
    LabelType,
    ReasonerContext,
)
from core.observation.registration import ReasonerRegistration, ReasonerRegistry

__all__ = [
    "AttributionSummary",
    "Contribution",
    "GateContext",
    "LabelType",
    "Observation",
    "ReasonerContext",
    "ReasonerRegistration",
    "ReasonerRegistry",
    "validate_observation",
]

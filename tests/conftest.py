"""Shared fixtures for the framework test suite — built on the stub reasoner.

Every fixture here is either framework-generic (gate verdicts and outputs)
or typed on the stub reasoner under ``tests/_stub_reasoner``. Nothing in this
file imports a real reasoner or a gate member, so a ``core/`` or ``app/`` test
can never depend on ATO or on a gate's private models by accident (gate
members keep their own fixtures under ``gates/<name>/tests``).

All fixtures produce valid, fully-constructed objects. Tests that need an
edge-case variant build one via ``model_copy(update=...)`` or construct the
deviation directly. The stub reasoner binds one advisory gate, the scripted
fake gate ``"fake"``, so the baseline submissions address exactly that gate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from app.reasoner_registry import LiveReasonerRegistry
from core.actions import DecisionAction
from core.gate import EvidenceRef, GateOutput, GateVerdict
from core.observation import (
    AttributionSummary,
    Contribution,
    GateContext,
    LabelType,
    ReasonerContext,
    ReasonerRegistration,
)
from tests._stub_reasoner.assembler import build_submission
from tests._stub_reasoner.events import StubEvent
from tests._stub_reasoner.registry import STUB_REGISTRATION

if TYPE_CHECKING:
    from core.submission import Submission

# ---------------------------------------------------------------------------
# Stub reasoner identity
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_registration() -> ReasonerRegistration:
    """The stub reasoner's registration (``reasoner_id="stub-reasoner"``)."""
    return STUB_REGISTRATION


@pytest.fixture
def stub_registry(stub_registration: ReasonerRegistration) -> LiveReasonerRegistry:
    """A registry that knows exactly one reasoner: the stub."""
    return LiveReasonerRegistry([stub_registration])


# ---------------------------------------------------------------------------
# Submission building blocks
# ---------------------------------------------------------------------------


@pytest.fixture
def signal() -> Contribution:
    """One attributed feature contribution for the stub scorer."""
    return Contribution(
        feature_name="widget_wobble",
        feature_value=1.0,
        method="shap",
        value=0.05,
    )


@pytest.fixture
def gate_context() -> GateContext:
    """The envelope for the stub's bound gate (the fake gate, default script)."""
    return GateContext(gate_id="fake", gate_config={})


@pytest.fixture
def reasoner_context(signal: Contribution) -> ReasonerContext:
    """Stub scorer evidence: low ``widget_score``, sufficient history."""
    return ReasonerContext(
        reasoner_id=STUB_REGISTRATION.reasoner_id,
        reasoner_name=STUB_REGISTRATION.reasoner_name,
        model_version="stub-scorer-v1.0.0",
        inference_latency_ms=2.5,
        label_type=LabelType.NUMERICAL,
        label_name="widget_score",
        label_value=0.15,
        feature_set={
            "widget_wobble": 1.0,
            "widget_age_days": 42,
            "region": "REGION_A",
            "thin_history": False,
        },
        attribution=AttributionSummary(feature_contributions=[signal]),
    )


@pytest.fixture
def stub_event() -> StubEvent:
    """The stub event every baseline submission is about."""
    return StubEvent(
        event_id="evt-test-001",
        timestamp=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        widget_id="widget-12345",
    )


@pytest.fixture
def submission(
    stub_event: StubEvent,
    reasoner_context: ReasonerContext,
    gate_context: GateContext,
) -> Submission:
    """A recorded stub submission: ``widget_score`` 0.15 → ``low-score`` → ALLOW."""
    return build_submission(stub_event, reasoner_context, gate_contexts=[gate_context])


@pytest.fixture
def gated_submission(
    stub_event: StubEvent,
    reasoner_context: ReasonerContext,
    gate_context: GateContext,
) -> Submission:
    """A gate-mode stub submission: ``widget_score`` 0.55, between the bands."""
    context = reasoner_context.model_copy(update={"label_value": 0.55})
    return build_submission(stub_event, context, gate_contexts=[gate_context])


# ---------------------------------------------------------------------------
# Gate artifacts (framework-generic types)
# ---------------------------------------------------------------------------


@pytest.fixture
def gate_verdict() -> GateVerdict:
    """A confident ALLOW verdict from the fake gate (universal fields only)."""
    return GateVerdict(
        gate_id="fake",
        permitted_actions=[DecisionAction.ALLOW],
        required_controls=[],
        confidence=0.92,
        escalate_to_human=False,
        escalation_reason=None,
    )


@pytest.fixture
def gate_output(gate_verdict: GateVerdict) -> GateOutput:
    """A non-deterministic fake-gate output: the ALLOW verdict, one ``REGION_A``
    evidence reference (so the jurisdiction tier runs and finds no conflict),
    and a small opaque extension."""
    return GateOutput(
        gate_id="fake",
        deterministic=False,
        verdict=gate_verdict,
        evidence=(
            EvidenceRef(
                source_id="STUB-POLICY-A",
                locator="2. Calibration Review",
                jurisdictions=frozenset({"REGION_A"}),
            ),
        ),
        evidence_snapshot={"retrieval_query": "calibration review"},
        extra={"rationale": "Low widget score. History sufficient."},
    )

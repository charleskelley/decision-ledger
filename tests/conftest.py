"""Shared fixtures for the framework test suite — built on the stub reasoner.

Every fixture here is either framework-generic (gate verdicts, citations,
snippets, prompt snapshots, token costs) or typed on the stub reasoner under
``tests/_stub_reasoner``. Nothing in this file imports a real reasoner, so a
``core/`` or ``app/`` test can never depend on ATO by accident.

All fixtures produce valid, fully-constructed objects. Tests that need an
edge-case variant build one via ``model_copy(update=...)`` or construct the
deviation directly.

Reasoner-specific trees (``tests/reasoner/``) keep their own ``conftest.py``
with that reasoner's fixtures; pytest's conftest scoping means those never
shadow these for framework tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.reasoner_registry import StaticReasonerRegistry
from core.actions import DecisionAction
from core.gate.policy import Citation, PolicyGateVerdict, PromptSnapshot, TokenCost
from core.observation import (
    AttributionSummary,
    Contribution,
    GateContext,
    LabelType,
    ReasonerContext,
    ReasonerRegistration,
)
from core.routes import GateRoute
from core.snippet import RetrievedSnippet
from tests._stub_reasoner.events import StubEvent
from tests._stub_reasoner.registry import STUB_REGISTRATION

# ---------------------------------------------------------------------------
# Stub reasoner identity
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_registration() -> ReasonerRegistration:
    """The stub reasoner's registration (``reasoner_id="stub-reasoner"``)."""
    return STUB_REGISTRATION


@pytest.fixture
def stub_registry(stub_registration: ReasonerRegistration) -> StaticReasonerRegistry:
    """A registry that knows exactly one reasoner: the stub."""
    return StaticReasonerRegistry([stub_registration])


# ---------------------------------------------------------------------------
# Observation building blocks
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
    """Policy-gate dispatch envelope using the stub template and ``REGION_A``."""
    return GateContext(
        gate_id="policy",
        gate_config={
            "template_id": "stub-v1",
            "template_vars": {
                "widget_score": "0.150",
                "thin_history": "False",
                "region": "REGION_A",
            },
            "jurisdictions": ["REGION_A"],
            "risk_tier": "STANDARD",
        },
    )


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
def observation(
    gate_context: GateContext, reasoner_context: ReasonerContext
) -> StubEvent:
    """A fully assembled fast-path (``FAST_PATH_ALLOW``) stub observation."""
    return StubEvent(
        event_id="evt-test-001",
        timestamp=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        widget_id="widget-12345",
        route=GateRoute.FAST_PATH_ALLOW,
        reasoner_context=reasoner_context,
        fast_path_rationale="widget_score=0.150 < 0.20 → FAST_PATH_ALLOW",
        gate_context=gate_context,
    )


# ---------------------------------------------------------------------------
# Gate and retrieval artifacts (framework-generic types)
# ---------------------------------------------------------------------------


@pytest.fixture
def citation() -> Citation:
    """A citation into the stub corpus."""
    return Citation(
        policy_id="STUB-POLICY-A",
        snippet=(
            "Widgets with a thin history SHALL be held for calibration review "
            "before any release."
        ),
        relevance="Directly supports the calibration review requirement.",
    )


@pytest.fixture
def prompt_snapshot() -> PromptSnapshot:
    """Prompt snapshot for the stub template."""
    return PromptSnapshot(
        template_id="stub-v1",
        version="1.0.0",
        template_text=(
            "You are a policy gate. Widget score: {widget_score}. "
            "Policy context:\n{policy_snippets}"
        ),
    )


@pytest.fixture
def gate_output(citation: Citation) -> PolicyGateVerdict:
    """A confident ALLOW verdict with one citation."""
    return PolicyGateVerdict(
        permitted_actions=[DecisionAction.ALLOW],
        required_controls=[],
        rationale=(
            "Low widget score. No anomalous signals detected. History sufficient."
        ),
        citations=[citation],
        confidence=0.92,
        escalate_to_human=False,
        escalation_reason=None,
    )


@pytest.fixture
def retrieved_snippet() -> RetrievedSnippet:
    """One retrieved chunk from the stub corpus (``REGION_A``)."""
    return RetrievedSnippet(
        document_id="STUB-POLICY-A",
        title="Stub Widget Policy A",
        version="1.0",
        jurisdiction="REGION_A",
        section_path="2. Calibration Review",
        text=(
            "Widgets with a thin history SHALL be held for calibration review "
            "before any release."
        ),
        relevance_score=0.91,
        retrieval_path="reranked",
    )


@pytest.fixture
def token_cost() -> TokenCost:
    """A representative LLM token-cost record."""
    return TokenCost(
        prompt_tokens=1240,
        completion_tokens=312,
        total_tokens=1552,
        cost_usd=0.0078,
        model="gpt-4o-2024-08-06",
    )

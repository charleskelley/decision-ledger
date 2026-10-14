"""Fixtures for the RAG policy gate member's tests.

The gate is reasoner-agnostic, so these tests use no reasoner package and
none of the framework's test doubles. A minimal local reasoner plugin
(``FixtureReasoner``) points at the assets under ``fixtures/`` — one prompt
template (``stub-v1``) and two policy documents (``STUB-POLICY-A`` in
``REGION_A``, ``STUB-POLICY-B`` in ``REGION_B``), copies of the framework
stub reasoner's assets — and a minimal local Observation
(``WidgetObservation``) carries its decisions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, computed_field

from core.observation import (
    GateContext,
    LabelType,
    ReasonerContext,
    ReasonerRegistration,
)
from core.routes import GateRoute
from policy_rag_gate.models import TokenCost
from policy_rag_gate.models.snippet import RetrievedSnippet

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
PROMPTS_DIR = FIXTURES_DIR / "prompts"
CORPUS_DIR = FIXTURES_DIR / "corpus"

#: The fixture reasoner's tenant id.
REASONER_ID = "stub-reasoner"

_NAMESPACE = UUID("8d0c5a3e-6f1b-5a7c-9e2d-4b3a2c1d0e9f")


class FixtureReasoner:
    """A minimal ``ReasonerPlugin``-shaped object for the gate's tests.

    The gate reads only ``registration.reasoner_id``, ``prompts_dir``, and
    ``corpus_dir`` from a composed reasoner; the rest satisfies the protocol.
    """

    def __init__(
        self,
        reasoner_id: str = REASONER_ID,
        *,
        prompts_dir: Path = PROMPTS_DIR,
        corpus_dir: Path = CORPUS_DIR,
    ) -> None:
        self.registration = ReasonerRegistration(
            reasoner_id=reasoner_id,
            reasoner_name=f"{reasoner_id} (fixture)",
            allowed_gate_ids=frozenset({"policy"}),
        )
        self.router = APIRouter(prefix=f"/api/v1/{reasoner_id}")
        self.prompts_dir = prompts_dir
        self.corpus_dir = corpus_dir
        self.raw_event_factory = dict

    def build_services(self, framework: Any) -> Any:
        del framework
        return MagicMock()


class WidgetObservation(BaseModel):
    """A minimal Observation: just the fields the framework protocol needs."""

    model_config = ConfigDict(strict=True, frozen=True)

    event_id: str
    entity_type: str = "widget"
    timestamp: datetime
    widget_id: str
    route: GateRoute = GateRoute.ROUTE_TO_GATE
    reasoner_context: ReasonerContext | None = None
    fast_path_rationale: str | None = None
    gate_context: GateContext

    @computed_field
    @property
    def entity_id(self) -> UUID:
        """Framework UUID derived from ``widget_id``."""
        return uuid.uuid5(_NAMESPACE, self.widget_id)


@pytest.fixture
def fixture_reasoner() -> FixtureReasoner:
    """The fixture reasoner over ``fixtures/prompts`` and ``fixtures/corpus``."""
    return FixtureReasoner()


@pytest.fixture
def reasoner_for() -> type[FixtureReasoner]:
    """``FixtureReasoner`` as a fixture (test modules cannot import conftest)."""
    return FixtureReasoner


@pytest.fixture
def observation() -> WidgetObservation:
    """A fixture-reasoner observation naming the policy gate."""
    return WidgetObservation(
        event_id="evt-policy-001",
        timestamp=datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
        widget_id="widget-1",
        reasoner_context=ReasonerContext(
            reasoner_id=REASONER_ID,
            reasoner_name="Fixture Reasoner",
            model_version="widget-scorer-v1",
            label_type=LabelType.NUMERICAL,
            label_name="widget_score",
            label_value=0.5,
            feature_set={"thin_history": False, "region": "REGION_A"},
        ),
        gate_context=GateContext(
            gate_id="policy",
            gate_config={
                "template_id": "stub-v1",
                "template_vars": {
                    "widget_score": "0.500",
                    "thin_history": "False",
                    "region": "REGION_A",
                },
                "retrieval_query": "calibration review",
                "jurisdictions": ["REGION_A"],
                "risk_tier": "STANDARD",
            },
        ),
    )


@pytest.fixture
def retrieved_snippet() -> RetrievedSnippet:
    """One retrieved chunk from the fixture corpus (``REGION_A``)."""
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

"""Fixtures for the RAG policy gate member's tests.

The gate is reasoner-agnostic, so these tests use no reasoner package and
none of the framework's test doubles. A minimal local reasoner plugin
(``FixtureReasoner``) points at the assets under ``fixtures/`` — one prompt
template (``stub-v1``) and two policy documents (``STUB-POLICY-A`` in
``REGION_A``, ``STUB-POLICY-B`` in ``REGION_B``), copies of the framework
stub reasoner's assets — and a minimal widget ``Submission`` carries its
decisions. Its registration binds the gate to one corpus, ``stub-policy``,
at tier 1 (DR-34).

``FakeProvider`` is an in-memory retrieval provider (DR-34): corpora of
``FakePassage`` items, answered in stored order under the evidence
contract, with switches that break the contract or fail, so the gate's
provider seam is tested without Postgres, Elasticsearch, or a model.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi import APIRouter

from core.actions import DecisionAction
from core.gate import Compare
from core.observation import (
    BypassRule,
    GateBinding,
    GateContext,
    LabelType,
    ReasonerContext,
    ReasonerRegistration,
)
from core.submission import Submission
from policy_rag_gate.corpus import (
    CorpusRef,
    CorpusState,
    PolicyBindingConfig,
    ProvenanceLevel,
)
from policy_rag_gate.models import RetrievedContext, TokenCost, content_hash
from policy_rag_gate.providers import ProviderResult

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
PROMPTS_DIR = FIXTURES_DIR / "prompts"
CORPUS_DIR = FIXTURES_DIR / "corpus"

#: The fixture reasoner's tenant id.
REASONER_ID = "stub-reasoner"

#: The corpus the fixture reasoner's binding names (its ``fixtures/corpus``).
CORPUS_ID = "stub-policy"

#: The fixture reasoner's binding configuration: one corpus at tier 1.
BINDING_CONFIG: dict[str, Any] = {"corpora": [{"corpus_id": CORPUS_ID, "tier": 1}]}

_NAMESPACE = UUID("8d0c5a3e-6f1b-5a7c-9e2d-4b3a2c1d0e9f")

#: The fixture reasoner's one bypass rule: a low score may be recorded ALLOW.
LOW_SCORE_RULE = BypassRule(
    rule_id="low-score",
    version="1",
    description="widget_score below 0.2",
    predicate=Compare(field="label_value", op="lt", value=0.2),
    action=DecisionAction.ALLOW,
)


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
            gates=(GateBinding(gate_id="policy", config=BINDING_CONFIG),),
            bypass_rules=(LOW_SCORE_RULE,),
        )
        self.router = APIRouter(prefix=f"/api/v1/{reasoner_id}")
        self.prompts_dir = prompts_dir
        self.corpus_dir = corpus_dir

    def build_services(self, framework: Any) -> Any:
        del framework
        return MagicMock()


@pytest.fixture
def fixture_reasoner() -> FixtureReasoner:
    """The fixture reasoner over ``fixtures/prompts`` and ``fixtures/corpus``."""
    return FixtureReasoner()


@pytest.fixture
def reasoner_for() -> type[FixtureReasoner]:
    """``FixtureReasoner`` as a fixture (test modules cannot import conftest)."""
    return FixtureReasoner


@pytest.fixture
def submission() -> Submission:
    """A fixture-reasoner gate request addressing the policy gate."""
    return Submission(
        reasoner_id=REASONER_ID,
        event_id="evt-policy-001",
        timestamp=datetime(2026, 10, 13, 12, 0, tzinfo=UTC),
        entity_id=uuid.uuid5(_NAMESPACE, "widget-1"),
        entity_type="widget",
        mode="gate",
        reasoner_context=ReasonerContext(
            reasoner_id=REASONER_ID,
            reasoner_name="Fixture Reasoner",
            model_version="widget-scorer-v1",
            label_type=LabelType.NUMERICAL,
            label_name="widget_score",
            label_value=0.5,
            feature_set={"thin_history": False, "region": "REGION_A"},
        ),
        gate_contexts=(
            GateContext(
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
        ),
        raw_event={"widget_id": "widget-1"},
    )


@pytest.fixture
def retrieved_context() -> RetrievedContext:
    """One retrieved passage from the fixture corpus (``REGION_A``)."""
    text = (
        "Widgets with a thin history SHALL be held for calibration review "
        "before any release."
    )
    return RetrievedContext(
        corpus_id=CORPUS_ID,
        provider="reference",
        tier=1,
        document_id="STUB-POLICY-A",
        title="Stub Widget Policy A",
        version="1.0",
        jurisdiction="REGION_A",
        section_path="2. Calibration Review",
        span=(120, 120 + len(text)),
        text=text,
        content_hash=content_hash(text),
        rank=1,
        scores={"rerank": 0.91},
        provider_chunk_id="chunk-1",
        provenance_level="pinned_version",
        document_metadata={"effective_date": "2024-01-01"},
    )


@pytest.fixture
def binding_config() -> dict[str, Any]:
    """The fixture reasoner's binding configuration (JSON, as registered)."""
    return {"corpora": [dict(c) for c in BINDING_CONFIG["corpora"]]}


@pytest.fixture
def stub_binding() -> PolicyBindingConfig:
    """The fixture reasoner's binding, validated."""
    return PolicyBindingConfig.model_validate(BINDING_CONFIG)


# ---------------------------------------------------------------------------
# FakeProvider — an in-memory retrieval provider
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakePassage:
    """One passage a fake corpus holds."""

    document_id: str
    text: str
    title: str = ""
    version: str | None = "1.0"
    jurisdiction: str | None = "REGION_A"
    section_path: str | None = "1. Scope"


class FakeProvider:
    """``RetrievalProvider`` over in-memory corpora.

    Args:
        corpora: ``corpus_id`` → its passages, in rank order.
        provider_id: The id bindings name it by.
        provenance_level: What ``describe`` reports.
        passage_provenance: What every passage carries (defaults to
            ``provenance_level``).
        ranking_requested: Reported ranking requested.
        ranking_applied: Reported ranking applied (defaults to requested).
        describe_error: Raised by ``describe``.
        retrieve_error: Raised by ``retrieve``.
        tamper: Rewrites a result before it is returned (contract tests).
    """

    def __init__(
        self,
        corpora: Mapping[str, Sequence[FakePassage]],
        *,
        provider_id: str = "reference",
        provenance_level: ProvenanceLevel = "pinned_version",
        passage_provenance: ProvenanceLevel | None = None,
        ranking_requested: str = "reranked",
        ranking_applied: str | None = None,
        describe_error: Exception | None = None,
        retrieve_error: Exception | None = None,
        tamper: Callable[[ProviderResult], object] | None = None,
    ) -> None:
        self._corpora = {k: tuple(v) for k, v in corpora.items()}
        self._id = provider_id
        self._provenance: ProvenanceLevel = provenance_level
        self._passage_provenance: ProvenanceLevel = (
            passage_provenance or provenance_level
        )
        self._requested = ranking_requested
        self._applied = ranking_applied or ranking_requested
        self._describe_error = describe_error
        self._retrieve_error = retrieve_error
        self._tamper = tamper
        self.calls: list[dict[str, Any]] = []
        self.described: list[str] = []
        self.closed = False

    @property
    def provider_id(self) -> str:
        return self._id

    def describe(self, corpus: CorpusRef) -> CorpusState:
        self.described.append(corpus.corpus_id)
        if self._describe_error is not None:
            raise self._describe_error
        if corpus.corpus_id not in self._corpora:
            msg = f"corpus {corpus.corpus_id!r} is not held by {self._id!r}"
            raise LookupError(msg)
        return CorpusState(
            corpus_id=corpus.corpus_id,
            provider=self._id,
            provider_version="fake-1",
            state_marker=f"fake:{corpus.corpus_id}:{len(self._corpora[corpus.corpus_id])}",
            provenance_level=self._provenance,
            document_count=len(
                {p.document_id for p in self._corpora[corpus.corpus_id]}
            ),
        )

    def retrieve(
        self,
        query: str,
        corpus: CorpusRef,
        *,
        k: int,
        jurisdictions: Sequence[str] | None,
        risk_tier: str | None,
    ) -> Any:
        self.calls.append(
            {
                "query": query,
                "corpus_id": corpus.corpus_id,
                "tier": corpus.tier,
                "k": k,
                "jurisdictions": None if jurisdictions is None else list(jurisdictions),
                "risk_tier": risk_tier,
            }
        )
        if self._retrieve_error is not None:
            raise self._retrieve_error
        passages = [
            p
            for p in self._corpora[corpus.corpus_id]
            if not jurisdictions or p.jurisdiction in jurisdictions
        ][:k]
        result = ProviderResult(
            state=self.describe(corpus),
            contexts=tuple(
                RetrievedContext(
                    corpus_id=corpus.corpus_id,
                    provider=self._id,
                    tier=corpus.tier,
                    document_id=p.document_id,
                    title=p.title or f"Policy {p.document_id}",
                    version=p.version,
                    jurisdiction=p.jurisdiction,
                    section_path=p.section_path,
                    text=p.text,
                    content_hash=content_hash(p.text),
                    rank=rank,
                    scores={"fake": 1.0 / rank},
                    provider_chunk_id=f"{corpus.corpus_id}/{p.document_id}/{rank}",
                    provenance_level=self._passage_provenance,
                )
                for rank, p in enumerate(passages, 1)
            ),
            ranking_requested=self._requested,
            ranking_applied=self._applied,
        )
        return self._tamper(result) if self._tamper is not None else result

    def close(self) -> None:
        self.closed = True


#: The fixture corpus as fake passages (two documents, two jurisdictions).
STUB_PASSAGES: tuple[FakePassage, ...] = (
    FakePassage(
        "STUB-POLICY-A",
        "Widgets with a thin history SHALL be held for calibration review.",
        title="Stub Widget Policy A",
        jurisdiction="REGION_A",
        section_path="2. Calibration Review",
    ),
    FakePassage(
        "STUB-POLICY-B",
        "Region B widgets require a second inspection before release.",
        title="Stub Widget Policy B",
        jurisdiction="REGION_B",
        section_path="3. Release",
    ),
)


@pytest.fixture
def fake_provider() -> type[FakeProvider]:
    """``FakeProvider`` as a fixture (test modules cannot import conftest)."""
    return FakeProvider


@pytest.fixture
def fake_passage() -> type[FakePassage]:
    """``FakePassage`` as a fixture."""
    return FakePassage


@pytest.fixture
def stub_provider() -> FakeProvider:
    """A fake reference provider holding the fixture corpus as ``stub-policy``."""
    return FakeProvider({CORPUS_ID: STUB_PASSAGES})


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

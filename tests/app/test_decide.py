"""Behavioral tests for ``app.decide.execute_decision`` — the framework half.

``execute_decision`` is the single handoff point between any reasoner and
the framework. These tests pin its intake contract and orchestration
without touching infrastructure or the LLM, using the stub reasoner from
``tests/_stub_reasoner`` so no ATO type is involved:

- Intake validation runs first, against the registry: an unregistered
  reasoner, a disallowed gate, or a disallowed prompt template is rejected
  before anything else happens, and nothing is persisted.
- On the fast path the retriever and gate are never called, and the
  bundle is still persisted through the store.
- On the gate path the registration resolved from the registry drives
  enforcement; a schema-failure gate result routes to HOLD via Tier 1, and
  retrieval is scoped to the observation's ``reasoner_id``.

Fakes are hand-written (no MagicMock) so a call on a path that must not
reach a collaborator fails loudly with a clear message.

Fixtures (``observation``, ``stub_registry``, ``stub_registration``) come
from the root ``tests/conftest.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from app.decide import execute_decision
from app.gate.policy.gate import GateResult
from app.reasoner_registry import StaticReasonerRegistry
from core.actions import DecisionAction
from core.exceptions import ObservationContractError, UnregisteredReasonerError
from core.gate.policy import PolicyGateInput, PolicyGateOutput, PromptSnapshot
from core.routes import GateRoute

if TYPE_CHECKING:
    from core.bundle import DecisionBundle
    from core.observation import ReasonerRegistration
    from core.snippet import RetrievedSnippet

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingStore:
    """Bundle store fake that records every ``write`` call."""

    def __init__(self) -> None:
        self.written: list[DecisionBundle] = []

    def write(self, bundle: DecisionBundle) -> None:
        self.written.append(bundle)


class _ForbiddenRetriever:
    """Retriever fake that must not be reached on the fast path."""

    def retrieve(self, **_: Any) -> tuple[list[RetrievedSnippet], str]:
        raise AssertionError("retriever.retrieve() must not be called on this path")


class _ForbiddenGate:
    """Gate fake that must not be reached on the fast path."""

    async def evaluate(self, *_: Any, **__: Any) -> GateResult:
        raise AssertionError("gate.evaluate() must not be called on this path")


class _RecordingRetriever:
    """Retriever fake returning no snippets and recording its call kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> tuple[list[RetrievedSnippet], str]:
        self.calls.append(kwargs)
        return [], "rrf_only"


class _SchemaFailureGate:
    """Gate fake returning a result whose verdict is ``None`` (schema failure).

    Exercises the gate-path branch of ``execute_decision`` without an LLM:
    enforcement Tier 1 must route the decision to HOLD.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def evaluate(self, observation: Any, snippets: Any, **_: Any) -> GateResult:
        self.calls += 1
        snapshot = PromptSnapshot(
            template_id="stub-v1", version="1.0.0", template_text="{policy_snippets}"
        )
        return GateResult(
            gate_input=PolicyGateInput(
                model_version="fake-model",
                prompt_template_id=snapshot.template_id,
                prompt_template_version=snapshot.version,
                corpus_version="corpus-test",
                rendered_prompt="rendered",
                prompt_snapshot=snapshot,
                template_vars={},
            ),
            gate_output=PolicyGateOutput(
                verdict=None, response_text=None, token_cost=None
            ),
            latency_ms=1.0,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INGESTION_TS = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)


def _registry_with(registration: ReasonerRegistration) -> StaticReasonerRegistry:
    return StaticReasonerRegistry([registration])


async def _run_fast_path(observation: Any, *, registry: Any, store: _RecordingStore):
    """Invoke execute_decision with collaborators that forbid gate-path calls."""
    return await execute_decision(
        observation=observation,
        query="",
        retriever=_ForbiddenRetriever(),  # type: ignore[arg-type]
        gate=_ForbiddenGate(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        registry=registry,
        corpus_version="corpus-test",
        ingestion_timestamp=_INGESTION_TS,
    )


def _gate_route(observation: Any) -> Any:
    return observation.model_copy(
        update={"route": GateRoute.ROUTE_TO_GATE, "fast_path_rationale": None}
    )


# ---------------------------------------------------------------------------
# Intake validation — runs before any collaborator is touched
# ---------------------------------------------------------------------------


async def test_execute_decision_rejects_unregistered_reasoner(
    observation, stub_registration
):
    store = _RecordingStore()
    registry = _registry_with(
        stub_registration.model_copy(update={"reasoner_id": "other-reasoner"})
    )

    with pytest.raises(UnregisteredReasonerError) as exc_info:
        await _run_fast_path(observation, registry=registry, store=store)

    assert exc_info.value.reasoner_id == "stub-reasoner"
    assert store.written == []


async def test_execute_decision_rejects_disallowed_prompt_template(
    observation, stub_registration
):
    # The fixture's gate_config names template_id "stub-v1"; register the
    # reasoner with a different allow-list so the convention check fires.
    store = _RecordingStore()
    registry = _registry_with(
        stub_registration.model_copy(
            update={"allowed_prompt_template_ids": frozenset({"other-template-v9"})}
        )
    )

    with pytest.raises(ObservationContractError) as exc_info:
        await _run_fast_path(observation, registry=registry, store=store)

    assert "template_id 'stub-v1'" in str(exc_info.value)
    assert store.written == []


async def test_execute_decision_rejects_disallowed_gate_id(
    observation, stub_registration
):
    store = _RecordingStore()
    registry = _registry_with(
        stub_registration.model_copy(
            update={"allowed_gate_ids": frozenset({"some-other-gate"})}
        )
    )

    with pytest.raises(ObservationContractError) as exc_info:
        await _run_fast_path(observation, registry=registry, store=store)

    assert "gate_id 'policy'" in str(exc_info.value)
    assert store.written == []


async def test_execute_decision_rejects_fast_path_without_rationale(
    observation, stub_registry
):
    # Shape contract (independent of the registry): a fast-path observation
    # must document why the gate was bypassed.
    store = _RecordingStore()
    obs = observation.model_copy(update={"fast_path_rationale": None})

    with pytest.raises(ObservationContractError, match="fast_path_rationale"):
        await _run_fast_path(obs, registry=stub_registry, store=store)

    assert store.written == []


# ---------------------------------------------------------------------------
# Fast path — registered reasoner
# ---------------------------------------------------------------------------


async def test_fast_path_persists_bundle_without_calling_retriever_or_gate(
    observation, stub_registry
):
    store = _RecordingStore()

    bundle = await _run_fast_path(observation, registry=stub_registry, store=store)

    # _ForbiddenRetriever / _ForbiddenGate would have raised if touched.
    assert store.written == [bundle]
    assert bundle.decision_action == DecisionAction.ALLOW
    assert bundle.gate_input is None
    assert bundle.gate_output is None
    assert bundle.retrieval_path == "skipped"
    assert bundle.retrieval_query == ""


async def test_fast_path_block_persists_block_action(observation, stub_registry):
    store = _RecordingStore()
    obs = observation.model_copy(
        update={
            "route": GateRoute.FAST_PATH_BLOCK,
            "fast_path_rationale": "widget_score=0.97 > 0.95 → FAST_PATH_BLOCK",
        }
    )

    bundle = await _run_fast_path(obs, registry=stub_registry, store=store)

    assert bundle.decision_action == DecisionAction.BLOCK
    assert store.written == [bundle]


async def test_fast_path_bundle_carries_identity_and_latency(
    observation, stub_registry
):
    store = _RecordingStore()

    bundle = await execute_decision(
        observation=observation,
        query="",
        retriever=_ForbiddenRetriever(),  # type: ignore[arg-type]
        gate=_ForbiddenGate(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        registry=stub_registry,
        corpus_version="corpus-test",
        ingestion_timestamp=_INGESTION_TS,
        upstream_latency={"features_ms": 1.5, "scorer_ms": 2.5},
        decision_id="dec-explicit",
        idempotency_key="idem-explicit",
    )

    assert bundle.decision_id == "dec-explicit"
    assert bundle.idempotency_key == "idem-explicit"
    assert bundle.ingestion_timestamp == _INGESTION_TS
    assert bundle.latency_breakdown["features_ms"] == 1.5
    assert bundle.latency_breakdown["scorer_ms"] == 2.5
    assert bundle.latency_breakdown["retrieval_ms"] == 0.0
    assert bundle.latency_breakdown["gate_ms"] == 0.0
    assert "enforcement_ms" in bundle.latency_breakdown


async def test_idempotency_key_defaults_to_event_id(observation, stub_registry):
    store = _RecordingStore()

    bundle = await _run_fast_path(observation, registry=stub_registry, store=store)

    assert bundle.idempotency_key == observation.event_id


async def test_fast_path_bundle_keeps_the_stub_event_as_raw_event(
    observation, stub_registry
):
    # The framework stores the reasoner's own Observation type verbatim; it
    # never translates it into a framework-owned event shape.
    store = _RecordingStore()

    bundle = await _run_fast_path(observation, registry=stub_registry, store=store)

    assert bundle.raw_event is observation
    assert bundle.raw_event.entity_type == "widget"


# ---------------------------------------------------------------------------
# Gate path — registration from the registry drives enforcement
# ---------------------------------------------------------------------------


async def test_gate_path_schema_failure_routes_to_hold_and_persists(
    observation, stub_registry
):
    store = _RecordingStore()
    retriever = _RecordingRetriever()
    gate = _SchemaFailureGate()

    bundle = await execute_decision(
        observation=_gate_route(observation),
        query="calibration review",
        retriever=retriever,  # type: ignore[arg-type]
        gate=gate,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        registry=stub_registry,
        corpus_version="corpus-test",
        ingestion_timestamp=_INGESTION_TS,
    )

    assert gate.calls == 1
    assert bundle.decision_action == DecisionAction.HOLD
    assert bundle.enforcement_rule_applied == "tier1_schema_failure"
    assert bundle.retrieval_query == "calibration review"
    assert store.written == [bundle]


async def test_gate_path_retrieval_is_tenant_scoped_to_the_reasoner(
    observation, stub_registry
):
    store = _RecordingStore()
    retriever = _RecordingRetriever()

    await execute_decision(
        observation=_gate_route(observation),
        query="calibration review",
        retriever=retriever,  # type: ignore[arg-type]
        gate=_SchemaFailureGate(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        registry=stub_registry,
        corpus_version="corpus-test",
        ingestion_timestamp=_INGESTION_TS,
    )

    assert len(retriever.calls) == 1
    call = retriever.calls[0]
    assert call["reasoner_id"] == "stub-reasoner"
    assert call["query"] == "calibration review"
    assert call["jurisdictions"] == ["REGION_A"]

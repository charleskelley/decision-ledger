"""Unit tests for the eval driver's gate wiring and the baseline capture's extraction.

Since DR-28 the policy gate owns its retrieval providers (DR-34), prompt
registry, LLM client, and per-reasoner profiles, so the eval
``PipelineDriver`` reaches them through the composed policy gate and injects
faults by swapping in a policy gate built around the failing piece.
``capture_baselines`` reads the gate's rationale, citations, and retrieved
contexts from its ``extra`` / ``evidence_snapshot``, and an authority case
from a tiered gate output. All of it runs here without infrastructure: the
driver is built without ``__init__`` around stub services, a stub retrieval
provider, and ATO's real prompt templates and registration (its binding
names ``ato-policy``).

Per project rules no LLM response is mocked: the policy gate's client either
fails the test if called or raises (the robustness fault-injection path), and
the run() rationale case uses a scripted gate that returns a finished
``GateOutput`` without any client at all.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.audit import StoredBundle
from app.client import LocalClient
from app.gates import ComposedGate
from app.plugin import FrameworkServices
from app.reasoner_registry import LiveReasonerRegistry
from ato_reasoner.eval.capture_baselines import (
    _bundle_to_authority_case,
    _bundle_to_case,
)
from ato_reasoner.eval.driver import PipelineDriver
from ato_reasoner.events import AuthMethod, AuthOutcome, Geolocation, LoginEvent
from ato_reasoner.features import AtoFeatureVector, WindowSpec
from ato_reasoner.plugin import PLUGIN, AtoServices
from ato_reasoner.registry import ATO_REGISTRATION
from ato_reasoner.scorer import ScorerOutput
from core.actions import DecisionAction
from core.exceptions import LedgerError
from core.gate import GateOutput, GateVerdict
from policy_rag_gate.config import ReasonerProfile
from policy_rag_gate.corpus import CorpusRef, CorpusState
from policy_rag_gate.gate import PolicyGate
from policy_rag_gate.models import (
    Citation,
    PolicyGateExtra,
    PolicyGateSnapshot,
    PromptSnapshot,
    RetrievedContext,
    content_hash,
)
from policy_rag_gate.plugin import PolicyGatePlugin
from policy_rag_gate.prompt_registry import YamlPromptRegistry
from policy_rag_gate.providers import ProviderRegistry, ProviderResult

# ---------------------------------------------------------------------------
# Fixtures and doubles
# ---------------------------------------------------------------------------


def _event() -> LoginEvent:
    return LoginEvent(
        event_id=f"evt-wiring-{uuid4().hex[:8]}",
        timestamp=datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
        account_id="acct-wiring",
        session_id="sess-wiring",
        ip_address="203.0.113.30",
        geolocation=Geolocation(
            latitude=37.77,
            longitude=-122.42,
            country="US",
            city="San Francisco",
            asn="AS7922",
        ),
        device_fingerprint="fp-wiring",
        user_agent="Mozilla/5.0",
        auth_method=AuthMethod.PASSWORD,
        outcome=AuthOutcome.SUCCESS,
    )


def _features() -> AtoFeatureVector:
    return AtoFeatureVector(
        account_id="acct-wiring",
        computed_at=datetime(2026, 10, 11, 12, 0, tzinfo=UTC),
        sparse_history=False,
        velocity_1min=1,
        velocity_5min=2,
        velocity_60min=5,
        velocity_1440min=15,
        ip_novelty=1.0,
        device_novelty=1.0,
        geo_novelty=0.0,
        impossible_travel=False,
        travel_speed_kmh=None,
        device_consistency_score=0.4,
        user_agent_consistency=0.9,
        windows=[
            WindowSpec(window_minutes=1, available=True, event_count=1),
            WindowSpec(window_minutes=5, available=True, event_count=2),
            WindowSpec(window_minutes=60, available=True, event_count=5),
            WindowSpec(window_minutes=1440, available=True, event_count=15),
        ],
    )


def _gate_routed_score() -> ScorerOutput:
    return ScorerOutput(
        entity_id=uuid4(),
        risk_score=0.61,
        top_signals=[],
        scorer_version="xgb-test",
        inference_latency_ms=1.0,
    )


_TEXT = "Verifiers SHALL implement rate limiting."


def _snippet(
    score: float = 0.7, *, corpus_id: str = "ato-policy", tier: int = 1, rank: int = 1
) -> RetrievedContext:
    return RetrievedContext(
        corpus_id=corpus_id,
        provider="reference",
        tier=tier,
        document_id="NIST-800-63B",
        title="Digital Identity Guidelines",
        version="3",
        jurisdiction="US_FEDERAL",
        section_path="5.2.2",
        text=_TEXT,
        content_hash=content_hash(_TEXT),
        rank=rank,
        scores={"rerank": score},
        provenance_level="pinned_version",
    )


class _StubRetriever:
    """A retrieval provider holding any corpus asked for, one passage each."""

    provider_id = "reference"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def describe(self, corpus: CorpusRef) -> CorpusState:
        return CorpusState(
            corpus_id=corpus.corpus_id,
            provider="reference",
            provider_version="stub",
            state_marker="stub",
            provenance_level="pinned_version",
        )

    def retrieve(self, query: str, corpus: CorpusRef, **kwargs: Any) -> ProviderResult:
        self.calls.append({"query": query, "corpus_id": corpus.corpus_id, **kwargs})
        return ProviderResult(
            state=self.describe(corpus),
            contexts=(_snippet(corpus_id=corpus.corpus_id, tier=corpus.tier),),
            ranking_requested="reranked",
            ranking_applied="rrf_only",
        )


class _ForbiddenClient:
    model = "never-called"

    async def complete_structured(self, **_: Any) -> Any:
        raise AssertionError("the composed gate's own client must not be called")


class _ScriptedPolicyGate:
    """Returns a finished policy-gate GateOutput; no LLM client involved."""

    def __init__(self, output: GateOutput) -> None:
        self.output = output

    async def evaluate(self, request: Any) -> GateOutput:
        return self.output

    def close(self) -> None:
        pass


def _composed(gate: Any) -> ComposedGate:
    return ComposedGate(plugin=PolicyGatePlugin(), gate=gate)


def _policy_gate(retriever: _StubRetriever) -> PolicyGate:
    return PolicyGate(
        client=_ForbiddenClient(),  # type: ignore[arg-type]
        prompt_registry=YamlPromptRegistry([PLUGIN.prompts_dir]),
        providers=ProviderRegistry.of(retriever),  # type: ignore[arg-type]
    )


class _Store:
    """Keeps written bundles; answers lookup as the real store does."""

    def __init__(self) -> None:
        self.written: list[Any] = []

    def write(self, bundle: Any) -> None:
        self.written.append(bundle)

    def lookup(self, decision_id: str) -> StoredBundle:
        bundle = next(b for b in self.written if b.decision_id == decision_id)
        return StoredBundle(
            decision_id=decision_id,
            reasoner_id=bundle.submission.reasoner_id,
            schema_version=2,
            payload=json.loads(bundle.model_dump_json()),
        )


def _driver(gates: dict[str, Any]) -> PipelineDriver:
    """A PipelineDriver around stub services, built without ``__init__``."""
    driver = PipelineDriver.__new__(PipelineDriver)
    feature_svc = MagicMock()
    feature_svc.compute.return_value = _features()
    scorer = MagicMock()
    scorer.score.return_value = _gate_routed_score()
    driver._services = AtoServices(
        feature_svc=feature_svc,
        scorer=scorer,
        template_id="ato-v4",
        redis=MagicMock(),
    )
    driver._framework = FrameworkServices(
        settings=MagicMock(),
        pg_conn=MagicMock(),
        store=_Store(),  # type: ignore[arg-type]
        registry=LiveReasonerRegistry([ATO_REGISTRATION]),
        gates=gates,
    )
    driver._client = LocalClient(driver._framework)
    return driver


def _written(driver: PipelineDriver) -> list[Any]:
    store = driver._framework.store
    assert isinstance(store, _Store)
    return store.written


# ---------------------------------------------------------------------------
# Reaching the composed policy gate
# ---------------------------------------------------------------------------


def test_the_driver_reaches_the_composed_policy_gate():
    gate = _policy_gate(_StubRetriever())
    driver = _driver({"policy": _composed(gate)})

    assert driver._policy_gate() is gate


@pytest.mark.parametrize(
    "gates",
    [{}, {"policy": _composed(_ScriptedPolicyGate(MagicMock()))}],
    ids=["not-composed", "not-a-policy-gate"],
)
def test_reaching_the_policy_gate_fails_loudly_when_it_is_not_composed(gates):
    with pytest.raises(RuntimeError, match="'policy' gate is not composed"):
        _driver(gates)._policy_gate()


def test_swapping_in_a_policy_gate_leaves_the_driver_and_other_gates_alone():
    original = _composed(_policy_gate(_StubRetriever()))
    rules = MagicMock(name="rules-composed-gate")
    driver = _driver({"policy": original, "rules": rules})
    replacement = _policy_gate(_StubRetriever())

    swapped = driver._framework_with_policy_gate(replacement)

    assert swapped.gates["policy"].gate is replacement
    assert swapped.gates["policy"].plugin is original.plugin
    assert swapped.gates["rules"] is rules
    assert driver._framework.gates["policy"] is original
    assert swapped.store is driver._framework.store


def test_swapping_in_a_policy_gate_keeps_the_deployment_mandate():
    # Under DECISIONLEDGER_MANDATORY_GATES=policy a fault-injected run must
    # still treat the gate as mandatory (a record would otherwise skip it).
    mandated = ComposedGate(
        plugin=PolicyGatePlugin(), gate=_policy_gate(_StubRetriever()), mandatory=True
    )
    driver = _driver({"policy": mandated})

    swapped = driver._framework_with_policy_gate(_policy_gate(_StubRetriever()))

    assert swapped.gates["policy"].mandatory is True


def test_a_fault_injected_gate_keeps_the_composed_gates_profiles():
    # The composed gate lets ATO use only ato-v1; the swapped-in faulty gate
    # must enforce the same profile, so the assembler's ato-v4 context is
    # refused at intake instead of slipping through the fault-injection run.
    profiles = {
        ATO_REGISTRATION.reasoner_id: ReasonerProfile(
            template_ids=frozenset({"ato-v1"}),
            jurisdictions=frozenset({"INTERNAL", "US_FEDERAL"}),
        )
    }
    gate = PolicyGate(
        client=_ForbiddenClient(),  # type: ignore[arg-type]
        prompt_registry=YamlPromptRegistry([PLUGIN.prompts_dir]),
        providers=ProviderRegistry.of(_StubRetriever()),  # type: ignore[arg-type]
        profiles=profiles,
    )
    driver = _driver({"policy": _composed(gate)})

    with pytest.raises(LedgerError, match="'ato-v4' is not a prompt") as err:
        asyncio.run(driver.run_with_forced_fallback(_event(), fallback_kind="llm_5xx"))

    assert err.value.code == "GATE_CONFIG_UNAUTHORIZED"
    assert _written(driver) == []


# ---------------------------------------------------------------------------
# Fault injection and forced paths
# ---------------------------------------------------------------------------


def test_forced_llm_5xx_runs_retrieval_then_holds_on_the_failed_call():
    retriever = _StubRetriever()
    driver = _driver({"policy": _composed(_policy_gate(retriever))})

    action = asyncio.run(
        driver.run_with_forced_fallback(_event(), fallback_kind="llm_5xx")
    )

    assert action == DecisionAction.HOLD
    # The gate retrieved from the corpus ATO's binding names, then failed
    # on the LLM call.
    (call,) = retriever.calls
    assert call["corpus_id"] == "ato-policy"
    assert call["k"] == 5
    [bundle] = _written(driver)
    policy = bundle.gate("policy")
    assert policy.status == "ran"
    assert policy.enforcement.enforcement_rule_applied == "tier1_schema_failure"
    extra = PolicyGateExtra.model_validate(policy.output.extra, strict=False)
    assert extra.failure == "llm_error"


def test_a_forced_retrieval_error_fails_the_gate_closed_and_holds():
    # The framework fails a raising gate closed (DR-29), so the decision is
    # still written, with the policy gate's record carrying the error.
    driver = _driver({"policy": _composed(_policy_gate(_StubRetriever()))})

    action = asyncio.run(
        driver.run_with_forced_fallback(_event(), fallback_kind="retrieval_error")
    )

    assert action == DecisionAction.HOLD
    [bundle] = _written(driver)
    policy = bundle.gate("policy")
    assert policy.status == "failed"
    assert policy.reason.startswith("raised _RetrievalForcedError: ")


@pytest.mark.parametrize("risk_score", [0.61, 0.05, 0.97])
def test_forced_schema_failure_holds_without_calling_the_gate(risk_score):
    # Every band, the recorded ones included: the path forces gate mode, or a
    # login in a band would be recorded and never reach the schema-failure
    # tier (the robustness check is zero-tolerance).
    retriever = _StubRetriever()
    driver = _driver({"policy": _composed(_policy_gate(retriever))})
    driver._services.scorer.score.return_value = _gate_routed_score().model_copy(
        update={"risk_score": risk_score}
    )

    action = asyncio.run(driver.run_with_forced_schema_failure(_event()))

    assert action == DecisionAction.HOLD
    assert retriever.calls == []
    assert _written(driver) == []


# ---------------------------------------------------------------------------
# run(): the consistency rationale comes from the policy gate's extra
# ---------------------------------------------------------------------------


def test_run_reports_the_rationale_from_the_policy_gates_extra():
    output = GateOutput(
        gate_id="policy",
        deterministic=False,
        verdict=GateVerdict(
            gate_id="policy",
            permitted_actions=[DecisionAction.CHALLENGE],
            required_controls=[],
            confidence=0.83,
            escalate_to_human=False,
            escalation_reason=None,
        ),
        extra=PolicyGateExtra(rationale="Device mismatch; step up.").model_dump(
            mode="json"
        ),
    )
    driver = _driver({"policy": _composed(_ScriptedPolicyGate(output))})

    result = asyncio.run(driver.run([_event()]))

    assert result.decision_action == DecisionAction.CHALLENGE
    assert result.confidence == 0.83
    assert result.rationale == "Device mismatch; step up."


# ---------------------------------------------------------------------------
# capture_baselines: the faithfulness case comes from the gate's extension
# ---------------------------------------------------------------------------


def _gate_path_bundle(*, verdict: bool = True, layout: str = "tiered_blocks") -> Any:
    citation = Citation(
        policy_id="NIST-800-63B", snippet="SHALL rate limit", relevance="why"
    )
    snapshot = PolicyGateSnapshot(
        retrieval_query="rate limiting",
        corpus_queries=[],
        # A raw cross-encoder logit can be negative; capture must cope.
        contexts=[_snippet(), _snippet(score=-4.32, rank=2)],
        evidence_layout=layout,
        dedup_merged=0,
        index_version="unknown",
        model_version="gpt-test",
        prompt_template_id="ato-v4",
        prompt_template_version="4.0.0",
        rendered_prompt="rendered",
        prompt_snapshot=PromptSnapshot(
            template_id="ato-v4", version="4.0.0", template_text="t"
        ),
        template_vars={},
    )
    output = GateOutput(
        gate_id="policy",
        deterministic=False,
        verdict=(
            GateVerdict(
                gate_id="policy",
                permitted_actions=[DecisionAction.CHALLENGE],
                required_controls=[],
                confidence=0.8,
                escalate_to_human=False,
                escalation_reason=None,
            )
            if verdict
            else None
        ),
        evidence_snapshot=snapshot.model_dump(mode="json"),
        extra=PolicyGateExtra(
            rationale="Rate limits apply." if verdict else None,
            citations=[citation] if verdict else [],
            governing_corpus_id="ato-policy" if verdict else None,
            conflict_detected=False if verdict else None,
            evidence_sufficient=True if verdict else None,
        ).model_dump(mode="json"),
    )
    return _bundle_with_policy_output(output)


def _bundle_with_policy_output(output: GateOutput | None) -> Any:
    """A bundle-shaped stand-in whose policy gate record carries ``output``."""
    record = SimpleNamespace(output=output)
    return SimpleNamespace(gate=lambda gate_id: record if gate_id == "policy" else None)


@pytest.mark.parametrize("round_trip", [False, True], ids=["in-memory", "stored"])
def test_capture_reads_rationale_citations_and_contexts_from_the_gate(round_trip):
    bundle = _gate_path_bundle()
    if round_trip:
        output = bundle.gate("policy").output
        bundle = _bundle_with_policy_output(
            GateOutput.model_validate_json(output.model_dump_json())
        )

    case = _bundle_to_case("device_fingerprint_anomaly", bundle)

    assert case == {
        "case_id": "f-cap-device_fingerprint_anomaly",
        "rationale": "Rate limits apply.",
        "contexts": [
            "Verifiers SHALL implement rate limiting.",
            "Verifiers SHALL implement rate limiting.",
        ],
        "cited_snippets": ["SHALL rate limit"],
    }


def test_capture_skips_recorded_and_verdictless_bundles():
    assert _bundle_to_case("x", _bundle_with_policy_output(None)) is None
    assert _bundle_to_case("x", _gate_path_bundle(verdict=False)) is None


def test_a_tiered_gate_output_becomes_an_authority_case():
    case = _bundle_to_authority_case("device_fingerprint_anomaly", _gate_path_bundle())

    assert case == {
        "case_id": "a-cap-device_fingerprint_anomaly",
        "governing_corpus_id": "ato-policy",
        "contexts": [
            {"corpus_id": "ato-policy", "tier": 1, "document_id": "NIST-800-63B"},
            {"corpus_id": "ato-policy", "tier": 1, "document_id": "NIST-800-63B"},
        ],
        "on_point_document_ids": ["NIST-800-63B"],
    }


def test_only_a_tiered_verdict_becomes_an_authority_case():
    assert (
        _bundle_to_authority_case("x", _gate_path_bundle(layout="numbered_list"))
        is None
    )
    assert _bundle_to_authority_case("x", _gate_path_bundle(verdict=False)) is None
    assert _bundle_to_authority_case("x", _bundle_with_policy_output(None)) is None

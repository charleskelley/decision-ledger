"""Unit tests for the eval driver's gate wiring and the baseline capture's extraction.

Since DR-28 the policy gate owns its retriever, prompt registry, and LLM
client, so the eval ``PipelineDriver`` reaches them through the composed
policy gate and injects faults by swapping in a policy gate built around the
failing piece. ``capture_baselines`` reads the gate's rationale, citations,
and retrieved contexts from its ``extra`` / ``evidence_snapshot``. All of it
runs here without infrastructure: the driver is built without ``__init__``
around stub services, a stub retriever, and ATO's real prompt templates.

Per project rules no LLM response is mocked: the policy gate's client either
fails the test if called or raises (the robustness fault-injection path), and
the run() rationale case uses a scripted gate that returns a finished
``GateOutput`` without any client at all.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.gate.policy.gate import PolicyGate
from app.gate.policy.plugin import PolicyGatePlugin
from app.gate.policy.prompt_registry import YamlPromptRegistry
from app.gates import ComposedGate
from app.plugin import FrameworkServices
from app.reasoner_registry import StaticReasonerRegistry
from ato_reasoner.eval.capture_baselines import _bundle_to_case
from ato_reasoner.eval.driver import PipelineDriver
from ato_reasoner.events import AuthMethod, AuthOutcome, Geolocation, LoginEvent
from ato_reasoner.features import AtoFeatureVector, WindowSpec
from ato_reasoner.plugin import PLUGIN, AtoServices
from ato_reasoner.registry import ATO_REGISTRATION
from ato_reasoner.scorer import ScorerOutput
from core.actions import DecisionAction
from core.gate import GateOutput, GateVerdict
from core.gate.policy import (
    Citation,
    PolicyGateExtra,
    PolicyGateSnapshot,
    PromptSnapshot,
)
from core.routes import GateRoute
from core.snippet import RetrievedSnippet

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
        route=GateRoute.ROUTE_TO_GATE,
    )


def _snippet(score: float = 0.7) -> RetrievedSnippet:
    return RetrievedSnippet(
        document_id="NIST-800-63B",
        title="Digital Identity Guidelines",
        version="3",
        jurisdiction="US_FEDERAL",
        section_path="5.2.2",
        text="Verifiers SHALL implement rate limiting.",
        relevance_score=score,
        retrieval_path="reranked",
    )


class _StubRetriever:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> tuple[list[RetrievedSnippet], str]:
        self.calls.append(kwargs)
        return [_snippet()], "rrf_only"


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
        retriever=retriever,  # type: ignore[arg-type]
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
        corpus_version="corpus-test",
        redis=MagicMock(),
    )
    driver._framework = FrameworkServices(
        settings=MagicMock(),
        pg_conn=MagicMock(),
        store=MagicMock(),
        registry=StaticReasonerRegistry([ATO_REGISTRATION]),
        gates=gates,
    )
    return driver


# ---------------------------------------------------------------------------
# Reaching the composed policy gate
# ---------------------------------------------------------------------------


def test_the_retriever_comes_from_the_composed_policy_gate():
    retriever = _StubRetriever()
    driver = _driver({"policy": _composed(_policy_gate(retriever))})

    assert driver.retriever is retriever


@pytest.mark.parametrize(
    "gates",
    [{}, {"policy": _composed(_ScriptedPolicyGate(MagicMock()))}],
    ids=["not-composed", "not-a-policy-gate"],
)
def test_reaching_the_policy_gate_fails_loudly_when_it_is_not_composed(gates):
    with pytest.raises(RuntimeError, match="'policy' gate is not composed"):
        _ = _driver(gates).retriever


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
    (call,) = retriever.calls
    assert call["reasoner_id"] == ATO_REGISTRATION.reasoner_id
    bundle = driver._framework.store.write.call_args[0][0]
    assert bundle.enforcement_rule_applied == "tier1_schema_failure"
    extra = PolicyGateExtra.model_validate(bundle.gate_output.extra, strict=False)
    assert extra.failure == "llm_error"


def test_forced_retrieval_error_maps_to_hold():
    driver = _driver({"policy": _composed(_policy_gate(_StubRetriever()))})

    action = asyncio.run(
        driver.run_with_forced_fallback(_event(), fallback_kind="retrieval_error")
    )

    assert action == DecisionAction.HOLD
    driver._framework.store.write.assert_not_called()


def test_forced_schema_failure_holds_without_calling_the_gate():
    retriever = _StubRetriever()
    driver = _driver({"policy": _composed(_policy_gate(retriever))})

    action = asyncio.run(driver.run_with_forced_schema_failure(_event()))

    assert action == DecisionAction.HOLD
    assert retriever.calls == []


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


def _gate_path_bundle(*, verdict: bool = True) -> Any:
    citation = Citation(
        policy_id="NIST-800-63B", snippet="SHALL rate limit", relevance="why"
    )
    snapshot = PolicyGateSnapshot(
        retrieval_query="rate limiting",
        # A raw cross-encoder logit can be negative; capture must cope.
        retrieval_results=[_snippet(), _snippet(score=-4.32)],
        retrieval_path="reranked",
        index_version="unknown",
        model_version="gpt-test",
        prompt_template_id="ato-v3",
        prompt_template_version="3.0.0",
        corpus_version="ato-v1",
        rendered_prompt="rendered",
        prompt_snapshot=PromptSnapshot(
            template_id="ato-v3", version="3.0.0", template_text="t"
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
        ).model_dump(mode="json"),
    )
    return MagicMock(gate_output=output)


@pytest.mark.parametrize("round_trip", [False, True], ids=["in-memory", "stored"])
def test_capture_reads_rationale_citations_and_contexts_from_the_gate(round_trip):
    bundle = _gate_path_bundle()
    if round_trip:
        stored = json.loads(json.dumps(bundle.gate_output.model_dump(mode="json")))
        bundle = MagicMock(gate_output=GateOutput.model_validate(stored, strict=False))

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


def test_capture_skips_fast_path_and_verdictless_bundles():
    assert _bundle_to_case("x", MagicMock(gate_output=None)) is None
    assert _bundle_to_case("x", _gate_path_bundle(verdict=False)) is None

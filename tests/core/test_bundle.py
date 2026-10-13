"""Behavioral tests for DecisionBundle and its component contracts.

DecisionBundle is the convergence point — it imports from every other core
module. These tests verify that the bundle accepts domain types through their
framework protocol interfaces, that its gate layer is the universal open
contract (DR-28: ``GateInput`` + ``GateOutput`` with an opaque extension, no
retrieval layers), and that the frozen/immutability contract holds.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.bundle import DecisionBundle
from core.gate import GateInput, GateOutput
from core.gate.policy import TokenCost
from core.snippet import RetrievedSnippet


def _bundle(observation, **overrides) -> DecisionBundle:
    base = {
        "decision_id": "dec-001",
        "created_at": datetime(2024, 1, 15, 10, 30, 2, tzinfo=UTC),
        "raw_event": observation,
        "idempotency_key": "sha256-abc123",
        "ingestion_timestamp": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        "queue_position": None,
        "gate_input": None,
        "gate_output": None,
        "decision_action": DecisionAction.ALLOW,
        "enforcement_rule_applied": None,
        "override_log": [],
        "latency_breakdown": {},
    }
    base.update(overrides)
    return DecisionBundle(**base)


def test_retrieved_snippet_construction(retrieved_snippet):
    assert retrieved_snippet.document_id == "STUB-POLICY-A"
    assert retrieved_snippet.relevance_score == 0.91
    assert retrieved_snippet.retrieval_path == "reranked"


def test_retrieved_snippet_accepts_a_negative_reranker_logit():
    # The cross-encoder reranker reports raw logits, which are negative for
    # poor matches; the score is in the retriever's own scheme (unbounded).
    snippet = RetrievedSnippet(
        document_id="STUB-POLICY-A",
        title="Stub Widget Policy A",
        version="1.0",
        jurisdiction="REGION_A",
        section_path="2. Calibration Review",
        text="Sample text.",
        relevance_score=-4.32,
        retrieval_path="reranked",
    )
    assert RetrievedSnippet.model_validate(snippet.model_dump(mode="json")) == snippet


def test_retrieved_snippet_is_immutable(retrieved_snippet):
    with pytest.raises(ValidationError):
        retrieved_snippet.document_id = "OTHER"


def test_token_cost_construction(token_cost):
    assert token_cost.total_tokens == (
        token_cost.prompt_tokens + token_cost.completion_tokens
    )


def test_token_cost_rejects_negative_values():
    with pytest.raises(ValidationError):
        TokenCost(
            prompt_tokens=-1,
            completion_tokens=100,
            total_tokens=99,
            cost_usd=0.001,
            model="gpt-4o",
        )


@pytest.mark.smoke
def test_decision_bundle_accepts_stub_event_as_observation(observation, gate_output):
    bundle = _bundle(
        observation,
        decision_id="dec-smoke-001",
        queue_position=1,
        gate_input=GateInput(gate_id="policy"),
        gate_output=gate_output,
        latency_breakdown={"ingestion_ms": 1.2, "gate_ms": 420.0},
    )
    assert bundle.decision_action == DecisionAction.ALLOW
    assert bundle.raw_event is observation
    gate_config = bundle.raw_event.gate_context.gate_config
    assert gate_config is not None
    assert gate_config["template_id"] == "stub-v1"
    assert bundle.raw_event.fast_path_rationale is not None
    assert "FAST_PATH_ALLOW" in bundle.raw_event.fast_path_rationale
    # The gate layer is the universal base; the gate's own record rides in
    # the opaque extension.
    assert type(bundle.gate_output) is GateOutput
    assert bundle.gate_output.verdict == gate_output.verdict
    assert bundle.gate_output.evidence_snapshot == {
        "retrieval_query": "calibration review"
    }


def test_decision_bundle_has_no_retrieval_layer_of_its_own():
    # DR-28: a retrieving gate records its retrieval inside its own
    # evidence_snapshot; the bundle carries no retrieval fields.
    fields = set(DecisionBundle.model_fields)
    assert not fields & {
        "retrieval_query",
        "retrieval_results",
        "retrieval_path",
        "index_version",
    }


def test_decision_bundle_no_valid_verdict_shape(observation):
    # gate_output is non-None; verdict is None; the gate's extra carries the
    # forensic record.
    bundle = _bundle(
        observation,
        decision_id="dec-gate-fail-001",
        gate_input=GateInput(gate_id="policy"),
        gate_output=GateOutput(
            gate_id="policy",
            deterministic=False,
            verdict=None,
            extra={"failure": "llm_error"},
        ),
        decision_action=DecisionAction.HOLD,
        enforcement_rule_applied="tier1_schema_failure",
        override_log=["tier1_schema_failure: fired — ..."],
    )
    assert bundle.gate_output is not None
    assert bundle.gate_output.verdict is None
    assert bundle.gate_output.extra == {"failure": "llm_error"}
    assert bundle.decision_action == DecisionAction.HOLD


def test_decision_bundle_fast_path_has_no_gate_artifacts(observation):
    bundle = _bundle(observation, override_log=["fast_path_allow"])
    assert bundle.gate_input is None
    assert bundle.gate_output is None


def test_decision_bundle_is_immutable(observation, gate_output):
    bundle = _bundle(
        observation,
        gate_input=GateInput(gate_id="policy"),
        gate_output=gate_output,
    )
    with pytest.raises(ValidationError):
        bundle.decision_action = DecisionAction.BLOCK

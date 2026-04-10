"""Behavioral tests for DecisionBundle and its component contracts.

DecisionBundle is the convergence point — it imports from every other core
module. These tests verify that the bundle accepts domain types through their
framework protocol interfaces and that the frozen/immutability contract holds.

Domain-specific provenance (feature snapshot, scorer output, scorer model
version) now travels in raw_event.gate_context and raw_event.fast_path_rationale.
The bundle schema is leaner and domain-agnostic at the top level.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.bundle import DecisionBundle, PolicySnippet, ReviewPacket, TokenCost


def test_policy_snippet_construction(policy_snippet):
    assert policy_snippet.policy_id == "NIST-800-63B"
    assert policy_snippet.relevance_score == 0.91
    assert policy_snippet.retrieval_path == "reranked"


def test_policy_snippet_rejects_negative_relevance_score():
    with pytest.raises(ValidationError):
        PolicySnippet(
            policy_id="NIST-800-63B",
            title="Digital Identity Guidelines",
            version="4.0",
            jurisdiction="US_FEDERAL",
            section_path="5.2.3",
            text="Sample text.",
            relevance_score=-0.1,
            retrieval_path="reranked",
        )


def test_policy_snippet_is_immutable(policy_snippet):
    with pytest.raises(ValidationError):
        policy_snippet.policy_id = "OTHER"


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


def test_review_packet_construction(review_packet, login_event):
    assert review_packet.entity_id == login_event.entity_id
    assert review_packet.priority == 0


def test_review_packet_rejects_risk_score_outside_unit_interval(login_event):
    with pytest.raises(ValidationError):
        ReviewPacket(
            decision_id="dec-001",
            entity_id=login_event.entity_id,
            created_at=datetime(2024, 1, 15, tzinfo=UTC),
            hold_reason="Test.",
            enforcement_rule=None,
            risk_score=1.1,
            top_signals=[],
        )


@pytest.mark.smoke
def test_decision_bundle_accepts_login_event_as_observation(
    login_event,
    policy_snippet,
    policy_gate_output,
    prompt_snapshot,
):
    # Domain provenance (fast_path_rationale, gate_context) travels in raw_event.
    # The bundle no longer carries feature_snapshot or scorer_output at the
    # top level.
    bundle = DecisionBundle(
        decision_id="dec-smoke-001",
        created_at=datetime(2024, 1, 15, 10, 30, 2, tzinfo=UTC),
        raw_event=login_event,
        idempotency_key="sha256-abc123",
        ingestion_timestamp=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        queue_position=1,
        retrieval_query="MFA requirements for new device login",
        retrieval_results=[policy_snippet],
        retrieval_path="reranked",
        index_version="corpus-v1.0",
        gate_input_snapshot=prompt_snapshot,
        rendered_prompt="<prompt>",
        raw_llm_response='{"permitted_actions": ["ALLOW"]}',
        gate_output=policy_gate_output,
        final_action=DecisionAction.ALLOW,
        enforcement_rule_applied=None,
        override_log=[],
        review_packet=None,
        gate_model_version="gpt-4o-2024-08-06",
        policy_corpus_version="corpus-v1.0",
        latency_breakdown={"ingestion_ms": 1.2, "gate_ms": 420.0},
        llm_token_cost=None,
    )
    assert bundle.final_action == DecisionAction.ALLOW
    assert bundle.raw_event is login_event
    # Domain provenance is accessible via the Observation
    assert bundle.raw_event.gate_context.prompt_template_id == "ato-v1"
    assert bundle.raw_event.fast_path_rationale is not None
    assert "FAST_PATH_ALLOW" in bundle.raw_event.fast_path_rationale


def test_decision_bundle_gate_output_may_be_none(login_event, prompt_snapshot):
    # None gate_output is valid — represents a schema validation failure.
    # Enforcement routes these to HOLD.
    bundle = DecisionBundle(
        decision_id="dec-gate-fail-001",
        created_at=datetime(2024, 1, 15, 10, 30, 2, tzinfo=UTC),
        raw_event=login_event,
        idempotency_key="sha256-abc999",
        ingestion_timestamp=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        queue_position=None,
        retrieval_query="velocity spike new device",
        retrieval_results=[],
        retrieval_path="skipped",
        index_version="corpus-v1.0",
        gate_input_snapshot=prompt_snapshot,
        rendered_prompt="<prompt>",
        raw_llm_response="unparseable llm output",
        gate_output=None,
        final_action=DecisionAction.HOLD,
        enforcement_rule_applied="schema_validation_failure",
        override_log=["schema_validation_failure: gate_output is None → HOLD"],
        review_packet=None,
        gate_model_version=None,
        policy_corpus_version="corpus-v1.0",
        latency_breakdown={"ingestion_ms": 1.1},
        llm_token_cost=None,
    )
    assert bundle.gate_output is None
    assert bundle.final_action == DecisionAction.HOLD


def test_decision_bundle_is_immutable(
    login_event,
    policy_snippet,
    policy_gate_output,
    prompt_snapshot,
):
    bundle = DecisionBundle(
        decision_id="dec-frozen-001",
        created_at=datetime(2024, 1, 15, 10, 30, 2, tzinfo=UTC),
        raw_event=login_event,
        idempotency_key="sha256-frozen",
        ingestion_timestamp=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        queue_position=None,
        retrieval_query="test",
        retrieval_results=[policy_snippet],
        retrieval_path="rrf_only",
        index_version="corpus-v1.0",
        gate_input_snapshot=prompt_snapshot,
        rendered_prompt="<prompt>",
        raw_llm_response="{}",
        gate_output=policy_gate_output,
        final_action=DecisionAction.ALLOW,
        enforcement_rule_applied=None,
        override_log=[],
        review_packet=None,
        gate_model_version="gpt-4o-2024-08-06",
        policy_corpus_version="corpus-v1.0",
        latency_breakdown={},
        llm_token_cost=None,
    )
    with pytest.raises(ValidationError):
        bundle.final_action = DecisionAction.BLOCK

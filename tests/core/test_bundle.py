"""Behavioral tests for DecisionBundle and its component contracts.

DecisionBundle is the convergence point — it imports from every other core
module. These tests verify that the bundle accepts domain types through their
framework protocol interfaces, that subclass-typed gate contracts work, and
that the frozen/immutability contract holds.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.actions import DecisionAction
from core.bundle import DecisionBundle
from core.gate.policy import PolicyGateInput, PolicyGateOutput, TokenCost
from core.snippet import RetrievedSnippet


def _gate_input(prompt_snapshot, *, rendered: str = "<prompt>") -> PolicyGateInput:
    return PolicyGateInput(
        model_version="gpt-4o-2024-08-06",
        prompt_template_id=prompt_snapshot.template_id,
        prompt_template_version=prompt_snapshot.version,
        corpus_version="corpus-v1.0",
        rendered_prompt=rendered,
        prompt_snapshot=prompt_snapshot,
        template_vars={},
    )


def _gate_output(verdict, *, raw: str | None = None) -> PolicyGateOutput:
    return PolicyGateOutput(
        verdict=verdict,
        response_text=raw,
        token_cost=None,
    )


def test_retrieved_snippet_construction(retrieved_snippet):
    assert retrieved_snippet.document_id == "NIST-800-63B"
    assert retrieved_snippet.relevance_score == 0.91
    assert retrieved_snippet.retrieval_path == "reranked"


def test_retrieved_snippet_rejects_negative_relevance_score():
    with pytest.raises(ValidationError):
        RetrievedSnippet(
            document_id="NIST-800-63B",
            title="Digital Identity Guidelines",
            version="4.0",
            jurisdiction="US_FEDERAL",
            section_path="5.2.3",
            text="Sample text.",
            relevance_score=-0.1,
            retrieval_path="reranked",
        )


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
def test_decision_bundle_accepts_login_event_as_observation(
    login_event,
    retrieved_snippet,
    policy_gate_output,
    prompt_snapshot,
):
    bundle = DecisionBundle(
        decision_id="dec-smoke-001",
        created_at=datetime(2024, 1, 15, 10, 30, 2, tzinfo=UTC),
        raw_event=login_event,
        idempotency_key="sha256-abc123",
        ingestion_timestamp=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        queue_position=1,
        retrieval_query="MFA requirements for new device login",
        retrieval_results=[retrieved_snippet],
        retrieval_path="reranked",
        index_version="corpus-v1.0",
        gate_input=_gate_input(prompt_snapshot),
        gate_output=_gate_output(
            policy_gate_output, raw='{"permitted_actions": ["ALLOW"]}'
        ),
        decision_action=DecisionAction.ALLOW,
        enforcement_rule_applied=None,
        override_log=[],
        latency_breakdown={"ingestion_ms": 1.2, "gate_ms": 420.0},
    )
    assert bundle.decision_action == DecisionAction.ALLOW
    assert bundle.raw_event is login_event
    gate_config = bundle.raw_event.gate_context.gate_config
    assert gate_config is not None
    assert gate_config["template_id"] == "ato-v1"
    assert bundle.raw_event.fast_path_rationale is not None
    assert "FAST_PATH_ALLOW" in bundle.raw_event.fast_path_rationale
    # Subclass-typed gate contracts survive on the bundle
    assert isinstance(bundle.gate_input, PolicyGateInput)
    assert bundle.gate_input.gate_id == "policy"
    assert isinstance(bundle.gate_output, PolicyGateOutput)
    assert bundle.gate_output.verdict is policy_gate_output


def test_decision_bundle_schema_failure_shape(login_event, prompt_snapshot):
    # gate_output is non-None; verdict is None; response_text populated.
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
        gate_input=_gate_input(prompt_snapshot),
        gate_output=_gate_output(None, raw="unparseable gate output"),
        decision_action=DecisionAction.HOLD,
        enforcement_rule_applied="schema_validation_failure",
        override_log=["schema_validation_failure: verdict is None → HOLD"],
        latency_breakdown={"ingestion_ms": 1.1},
    )
    assert bundle.gate_output is not None
    assert bundle.gate_output.verdict is None
    assert isinstance(bundle.gate_output, PolicyGateOutput)
    assert bundle.gate_output.response_text == "unparseable gate output"
    assert bundle.decision_action == DecisionAction.HOLD


def test_decision_bundle_fast_path_has_no_gate_artifacts(login_event):
    bundle = DecisionBundle(
        decision_id="dec-fast-001",
        created_at=datetime(2024, 1, 15, 10, 30, 2, tzinfo=UTC),
        raw_event=login_event,
        idempotency_key="sha256-fast",
        ingestion_timestamp=datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        queue_position=None,
        retrieval_query="",
        retrieval_results=[],
        retrieval_path="skipped",
        index_version="unknown",
        gate_input=None,
        gate_output=None,
        decision_action=DecisionAction.ALLOW,
        enforcement_rule_applied=None,
        override_log=["fast_path_allow"],
        latency_breakdown={},
    )
    assert bundle.gate_input is None
    assert bundle.gate_output is None


def test_decision_bundle_is_immutable(
    login_event,
    retrieved_snippet,
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
        retrieval_results=[retrieved_snippet],
        retrieval_path="rrf_only",
        index_version="corpus-v1.0",
        gate_input=_gate_input(prompt_snapshot),
        gate_output=_gate_output(policy_gate_output, raw="{}"),
        decision_action=DecisionAction.ALLOW,
        enforcement_rule_applied=None,
        override_log=[],
        latency_breakdown={},
    )
    with pytest.raises(ValidationError):
        bundle.decision_action = DecisionAction.BLOCK

"""Tests for app/audit/bundle.py — pure DecisionBundle assembly.

Tests are pure (no I/O) and verify that build_bundle() correctly maps all
pipeline layer outputs into the DecisionBundle fields. Tests cover the
fast-path (no gate invocation) and gate-path cases, plus field defaults.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.audit.bundle import build_bundle
from app.enforcement.resolver import resolve
from core.actions import DecisionAction
from core.gate.policy import (
    PolicyGateInput,
    PolicyGateOutput,
    PromptSnapshot,
    TokenCost,
)
from core.routes import GateRoute
from core.snippet import RetrievedSnippet

# ---------------------------------------------------------------------------
# Helpers / local fixtures
# ---------------------------------------------------------------------------


def _snippet(document_id: str = "DOC-001") -> RetrievedSnippet:
    return RetrievedSnippet(
        document_id=document_id,
        title=f"Policy {document_id}",
        version="1.0",
        jurisdiction="US_FEDERAL",
        section_path="Section 1",
        text="All verifiers SHALL require MFA.",
        relevance_score=0.85,
        retrieval_path="reranked",
    )


def _token_cost() -> TokenCost:
    return TokenCost(
        prompt_tokens=500,
        completion_tokens=120,
        total_tokens=620,
        cost_usd=0.002450,
        model="gpt-4o-2024-08-06",
    )


_DECISION_ID = "dec-audit-001"
_IDEMPOTENCY_KEY = "sha256-abc123"
_INGESTION_TS = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)


def _gate_input(
    prompt_snapshot: PromptSnapshot,
    *,
    rendered: str,
    corpus_version: str = "corpus-2024-Q1",
) -> PolicyGateInput:
    return PolicyGateInput(
        model_version="gpt-4o-2024-08-06",
        prompt_template_id=prompt_snapshot.template_id,
        prompt_template_version=prompt_snapshot.version,
        corpus_version=corpus_version,
        rendered_prompt=rendered,
        prompt_snapshot=prompt_snapshot,
        template_vars={},
    )


def _gate_output(
    verdict, *, raw: str | None, with_tokens: bool = True
) -> PolicyGateOutput:
    return PolicyGateOutput(
        verdict=verdict,
        response_text=raw,
        token_cost=_token_cost() if with_tokens else None,
    )


# ---------------------------------------------------------------------------
# Fast-path bundle assembly
# ---------------------------------------------------------------------------


def test_build_bundle_fast_path_gate_artifacts_are_none(login_event):
    enf = resolve(login_event, None, snippets=[], decision_id=_DECISION_ID)
    bundle = build_bundle(
        decision_id=_DECISION_ID,
        obs=login_event,
        idempotency_key=_IDEMPOTENCY_KEY,
        ingestion_timestamp=_INGESTION_TS,
        enforcement_decision=enf,
    )
    # Fast path — gate never invoked: gate_input and gate_output are both None.
    assert bundle.gate_input is None
    assert bundle.gate_output is None


def test_build_bundle_fast_path_retrieval_defaults(login_event):
    enf = resolve(login_event, None, snippets=[], decision_id=_DECISION_ID)
    bundle = build_bundle(
        decision_id=_DECISION_ID,
        obs=login_event,
        idempotency_key=_IDEMPOTENCY_KEY,
        ingestion_timestamp=_INGESTION_TS,
        enforcement_decision=enf,
    )
    assert bundle.retrieval_query == ""
    assert bundle.retrieval_results == []
    assert bundle.retrieval_path == "skipped"
    assert bundle.index_version == "unknown"


def test_build_bundle_fast_path_identity_fields(login_event):
    enf = resolve(login_event, None, snippets=[], decision_id=_DECISION_ID)
    bundle = build_bundle(
        decision_id=_DECISION_ID,
        obs=login_event,
        idempotency_key=_IDEMPOTENCY_KEY,
        ingestion_timestamp=_INGESTION_TS,
        queue_position=42,
        enforcement_decision=enf,
    )
    assert bundle.decision_id == _DECISION_ID
    assert bundle.idempotency_key == _IDEMPOTENCY_KEY
    assert bundle.ingestion_timestamp == _INGESTION_TS
    assert bundle.queue_position == 42
    assert bundle.raw_event is login_event


def test_build_bundle_fast_path_decision_action_is_allow(login_event):
    # login_event route=FAST_PATH_ALLOW → enforcement returns ALLOW
    enf = resolve(login_event, None, snippets=[], decision_id=_DECISION_ID)
    bundle = build_bundle(
        decision_id=_DECISION_ID,
        obs=login_event,
        idempotency_key=_IDEMPOTENCY_KEY,
        ingestion_timestamp=_INGESTION_TS,
        enforcement_decision=enf,
    )
    assert bundle.decision_action == DecisionAction.ALLOW
    assert bundle.enforcement_rule_applied is None


def test_build_bundle_created_at_is_utc_and_recent(login_event):
    enf = resolve(login_event, None, snippets=[], decision_id=_DECISION_ID)
    before = datetime.now(UTC)
    bundle = build_bundle(
        decision_id=_DECISION_ID,
        obs=login_event,
        idempotency_key=_IDEMPOTENCY_KEY,
        ingestion_timestamp=_INGESTION_TS,
        enforcement_decision=enf,
    )
    after = datetime.now(UTC)
    assert bundle.created_at.tzinfo is not None
    assert before <= bundle.created_at <= after


# ---------------------------------------------------------------------------
# Gate-path bundle assembly
# ---------------------------------------------------------------------------


@pytest.fixture
def prompt_snapshot() -> PromptSnapshot:
    return PromptSnapshot(
        template_id="ato-v1",
        version="1.0.0",
        template_text="Risk: {risk_score}\n{policy_snippets}",
    )


@pytest.fixture
def gate_route_event(login_event):
    """LoginEvent routed to policy gate (ROUTE_TO_GATE)."""
    return login_event.model_copy(
        update={
            "route": GateRoute.ROUTE_TO_GATE,
            "fast_path_rationale": None,
        }
    )


def _make_gate_result(gate_output, prompt_snapshot, *, raw_response: str = ""):
    """Construct a GateResult carrying populated PolicyGateInput / PolicyGateOutput."""
    from app.gate.policy.gate import GateResult

    rendered = "Risk: 0.55\nSection 1: All verifiers SHALL require MFA."
    return GateResult(
        gate_input=_gate_input(prompt_snapshot, rendered=rendered),
        gate_output=_gate_output(
            gate_output,
            raw=raw_response or '{"permitted_actions": ["ALLOW"]}',
            with_tokens=True,
        ),
        latency_ms=1234.5,
    )


def test_build_bundle_gate_path_populates_gate_artifacts(
    gate_route_event, gate_output, prompt_snapshot
):
    snippets = [_snippet("NIST-800-63B")]
    enf = resolve(
        gate_route_event, gate_output, snippets=snippets, decision_id=_DECISION_ID
    )
    gr = _make_gate_result(gate_output, prompt_snapshot)
    bundle = build_bundle(
        decision_id=_DECISION_ID,
        obs=gate_route_event,
        idempotency_key=_IDEMPOTENCY_KEY,
        ingestion_timestamp=_INGESTION_TS,
        retrieval_query="impossible travel MFA block",
        retrieval_results=snippets,
        retrieval_path="reranked",
        index_version="v2.1",
        gate_result=gr,
        enforcement_decision=enf,
    )
    assert bundle.gate_input is gr.gate_input
    assert bundle.gate_output is gr.gate_output
    assert isinstance(bundle.gate_input, PolicyGateInput)
    assert bundle.gate_input.gate_id == "policy"
    assert bundle.gate_input.model_version == "gpt-4o-2024-08-06"
    assert bundle.gate_input.corpus_version == "corpus-2024-Q1"
    assert isinstance(bundle.gate_output, PolicyGateOutput)
    assert bundle.gate_output.verdict is gate_output
    assert bundle.gate_output.token_cost is not None
    assert bundle.gate_output.token_cost.total_tokens == 620


def test_build_bundle_gate_path_retrieval_fields(
    gate_route_event, gate_output, prompt_snapshot
):
    snippets = [_snippet("NIST-800-63B"), _snippet("HIPAA-164")]
    enf = resolve(
        gate_route_event, gate_output, snippets=snippets, decision_id=_DECISION_ID
    )
    gr = _make_gate_result(gate_output, prompt_snapshot)
    bundle = build_bundle(
        decision_id=_DECISION_ID,
        obs=gate_route_event,
        idempotency_key=_IDEMPOTENCY_KEY,
        ingestion_timestamp=_INGESTION_TS,
        retrieval_query="mfa authentication requirement",
        retrieval_results=snippets,
        retrieval_path="reranked",
        index_version="v2.1",
        gate_result=gr,
        enforcement_decision=enf,
    )
    assert bundle.retrieval_query == "mfa authentication requirement"
    assert len(bundle.retrieval_results) == 2
    assert bundle.retrieval_path == "reranked"
    assert bundle.index_version == "v2.1"


def test_build_bundle_schema_failure_routes_to_hold(gate_route_event, prompt_snapshot):
    """Gate verdict None → enforcement tier1_schema_failure → HOLD."""
    snippets: list[RetrievedSnippet] = []
    enf = resolve(gate_route_event, None, snippets=snippets, decision_id=_DECISION_ID)
    from app.gate.policy.gate import GateResult

    gr = GateResult(
        gate_input=_gate_input(prompt_snapshot, rendered="Risk: 0.55"),
        gate_output=_gate_output(None, raw="not valid json", with_tokens=False),
        latency_ms=500.0,
    )
    bundle = build_bundle(
        decision_id=_DECISION_ID,
        obs=gate_route_event,
        idempotency_key=_IDEMPOTENCY_KEY,
        ingestion_timestamp=_INGESTION_TS,
        gate_result=gr,
        enforcement_decision=enf,
    )
    assert bundle.decision_action == DecisionAction.HOLD
    assert bundle.enforcement_rule_applied == "tier1_schema_failure"
    # Schema-failure invariant: gate_output is non-None but verdict is None;
    # response_text carries the gate's emitted text for HOLD review.
    assert isinstance(bundle.gate_output, PolicyGateOutput)
    assert bundle.gate_output.verdict is None
    assert bundle.gate_output.response_text == "not valid json"
    assert isinstance(bundle.gate_input, PolicyGateInput)
    assert bundle.gate_input.rendered_prompt == "Risk: 0.55"


def test_build_bundle_latency_breakdown_stored(login_event):
    enf = resolve(login_event, None, snippets=[], decision_id=_DECISION_ID)
    latency = {"ingestion_ms": 5.2, "features_ms": 3.1, "scorer_ms": 1.8}
    bundle = build_bundle(
        decision_id=_DECISION_ID,
        obs=login_event,
        idempotency_key=_IDEMPOTENCY_KEY,
        ingestion_timestamp=_INGESTION_TS,
        enforcement_decision=enf,
        latency_breakdown=latency,
    )
    assert bundle.latency_breakdown["ingestion_ms"] == pytest.approx(5.2)
    assert bundle.latency_breakdown["scorer_ms"] == pytest.approx(1.8)


def test_build_bundle_override_log_from_enforcement(login_event):
    enf = resolve(login_event, None, snippets=[], decision_id=_DECISION_ID)
    bundle = build_bundle(
        decision_id=_DECISION_ID,
        obs=login_event,
        idempotency_key=_IDEMPOTENCY_KEY,
        ingestion_timestamp=_INGESTION_TS,
        enforcement_decision=enf,
    )
    # Fast path — single log entry
    assert len(bundle.override_log) == 1
    assert "fast_path_allow" in bundle.override_log[0]

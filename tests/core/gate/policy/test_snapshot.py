"""Behavioral tests for PolicyGateSnapshot — the policy gate's evidence snapshot.

The snapshot replaces both the bundle's former retrieval layers and the
``PolicyGateInput`` subclass (DR-28). It must carry every artifact an auditor
needs to rebuild the rendered prompt without the registry or the corpus, and
it must round-trip through the opaque ``GateOutput.evidence_snapshot`` JSON.
"""

import json

import pytest
from pydantic import ValidationError

from core.gate import GateOutput
from core.gate.policy import PolicyGateSnapshot, PromptSnapshot
from core.snippet import RetrievedSnippet


def _prompt_snapshot() -> PromptSnapshot:
    return PromptSnapshot(
        template_id="stub-v1",
        version="1.0.0",
        template_text="Risk: {risk_score}\n{policy_snippets}",
    )


def _snippet() -> RetrievedSnippet:
    return RetrievedSnippet(
        document_id="STUB-POLICY-A",
        title="Stub Widget Policy A",
        version="1.0",
        jurisdiction="REGION_A",
        section_path="2. Calibration Review",
        text="Widgets with a thin history SHALL be held.",
        relevance_score=0.91,
        retrieval_path="reranked",
    )


def _make(**overrides) -> PolicyGateSnapshot:
    base = {
        "retrieval_query": "calibration review",
        "retrieval_results": [_snippet()],
        "retrieval_path": "reranked",
        "index_version": "unknown",
        "model_version": "gpt-4o-2024-08-06",
        "prompt_template_id": "stub-v1",
        "prompt_template_version": "1.0.0",
        "corpus_version": "corpus-v1",
        "rendered_prompt": "Risk: 0.50\n[1] Stub Widget Policy A ...",
        "prompt_snapshot": _prompt_snapshot(),
        "template_vars": {"risk_score": "0.50"},
    }
    base.update(overrides)
    return PolicyGateSnapshot(**base)


def test_snapshot_carries_the_retrieval_and_the_prompt():
    snapshot = _make()
    assert snapshot.retrieval_query == "calibration review"
    assert snapshot.retrieval_results[0].document_id == "STUB-POLICY-A"
    assert snapshot.retrieval_path == "reranked"
    assert snapshot.prompt_template_id == "stub-v1"
    assert snapshot.prompt_snapshot.template_text.startswith("Risk:")
    assert snapshot.template_vars == {"risk_score": "0.50"}


def test_snapshot_round_trips_through_the_gate_output_extension():
    snapshot = _make()
    output = GateOutput(
        gate_id="policy",
        deterministic=False,
        verdict=None,
        evidence_snapshot=snapshot.model_dump(mode="json"),
    )
    stored = json.loads(json.dumps(output.model_dump(mode="json")))
    reloaded = GateOutput.model_validate(stored, strict=False)

    assert (
        PolicyGateSnapshot.model_validate(reloaded.evidence_snapshot, strict=False)
        == snapshot
    )


def test_snapshot_requires_every_audit_artifact():
    with pytest.raises(ValidationError, match="prompt_snapshot"):
        PolicyGateSnapshot(
            retrieval_query="q",
            retrieval_results=[],
            retrieval_path="skipped",
            index_version="unknown",
            model_version="m",
            prompt_template_id="stub-v1",
            prompt_template_version="1.0.0",
            corpus_version="c",
            rendered_prompt="",
            template_vars={},
        )  # type: ignore[call-arg]


def test_snapshot_is_frozen():
    snapshot = _make()
    with pytest.raises(ValidationError):
        snapshot.rendered_prompt = "tampered"

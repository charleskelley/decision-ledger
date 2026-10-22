"""Behavioral tests for PolicyGateSnapshot (v2) — the policy gate's evidence snapshot.

The snapshot replaces both the bundle's former retrieval layers and the
``PolicyGateInput`` subclass (DR-28). Version 2 (DR-34) records one
``CorpusQuery`` per bound corpus (its reference, the state its provider
reported, the quota, the filters, the ranking requested and applied), the
passages rendered under the evidence contract, the layout, and the merges.
It must carry every artifact an auditor needs to rebuild the rendered prompt
without the registry, a provider, or the corpus, and it must round-trip
through the opaque ``GateOutput.evidence_snapshot`` JSON.
"""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core.gate import GateOutput
from policy_rag_gate.corpus import CorpusRef, CorpusState
from policy_rag_gate.models import (
    CorpusQuery,
    PolicyGateSnapshot,
    PromptSnapshot,
    RetrievalFilters,
    RetrievedContext,
    content_hash,
)


def _prompt_snapshot() -> PromptSnapshot:
    return PromptSnapshot(
        template_id="stub-v1",
        version="1.0.0",
        template_text="Risk: {risk_score}\n{policy_snippets}",
    )


def _context(tier: int = 1, corpus_id: str = "stub-policy") -> RetrievedContext:
    text = "Widgets with a thin history SHALL be held."
    return RetrievedContext(
        corpus_id=corpus_id,
        provider="reference",
        tier=tier,
        document_id="STUB-POLICY-A",
        title="Stub Widget Policy A",
        version="1.0",
        jurisdiction="REGION_A",
        section_path="2. Calibration Review",
        text=text,
        content_hash=content_hash(text),
        rank=1,
        scores={"rerank": 0.91},
        provenance_level="pinned_version",
    )


def _query(corpus_id: str = "stub-policy", tier: int = 1) -> CorpusQuery:
    return CorpusQuery(
        corpus_ref=CorpusRef(corpus_id=corpus_id, tier=tier),
        state=CorpusState(
            corpus_id=corpus_id,
            provider="reference",
            provider_version="reference-1",
            state_marker="a" * 64,
            provenance_level="pinned_version",
            document_count=2,
            updated_at=datetime(2026, 10, 21, 9, 30, tzinfo=UTC),
        ),
        k=5,
        filters=RetrievalFilters(jurisdictions=("REGION_A",), risk_tier="STANDARD"),
        ranking_requested="reranked",
        ranking_applied="reranked",
        duration_ms=12.5,
        returned=1,
        merged=0,
    )


def _make(**overrides) -> PolicyGateSnapshot:
    base = {
        "retrieval_query": "calibration review",
        "corpus_queries": [_query()],
        "contexts": [_context()],
        "evidence_layout": "numbered_list",
        "dedup_merged": 0,
        "index_version": "unknown",
        "model_version": "gpt-4o-2024-08-06",
        "prompt_template_id": "stub-v1",
        "prompt_template_version": "1.0.0",
        "rendered_prompt": "Risk: 0.50\n[1] Stub Widget Policy A ...",
        "prompt_snapshot": _prompt_snapshot(),
        "template_vars": {"risk_score": "0.50"},
    }
    base.update(overrides)
    return PolicyGateSnapshot(**base)


def test_snapshot_carries_each_corpus_query_the_passages_and_the_prompt():
    snapshot = _make()
    assert snapshot.snapshot_version == 2
    assert snapshot.retrieval_query == "calibration review"
    (query,) = snapshot.corpus_queries
    assert query.corpus_ref.corpus_id == "stub-policy"
    assert query.state is not None
    assert query.state.state_marker == "a" * 64
    assert query.ranking_applied == "reranked"
    assert snapshot.contexts[0].content_hash == content_hash(snapshot.contexts[0].text)
    assert snapshot.prompt_template_id == "stub-v1"
    assert snapshot.prompt_snapshot.template_text.startswith("Risk:")
    assert snapshot.template_vars == {"risk_score": "0.50"}


def test_snapshot_round_trips_through_the_gate_output_extension():
    snapshot = _make(
        corpus_queries=[_query("regulatory", 1), _query("practice", 2)],
        contexts=[_context(1, "regulatory"), _context(2, "practice")],
        evidence_layout="tiered_blocks",
        dedup_merged=1,
    )
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


def test_a_corpus_that_was_not_queried_is_recorded_with_why():
    query = CorpusQuery(
        corpus_ref=CorpusRef(corpus_id="eu-only", tier=1, jurisdictions={"EU"}),
        state=None,
        k=5,
        filters=RetrievalFilters(),
        ranking_requested=None,
        ranking_applied="skipped",
        duration_ms=0.0,
        returned=0,
        merged=0,
        skipped="the binding reads ['EU'] from this corpus and the submission "
        "filters to ['US']",
    )

    snapshot = _make(corpus_queries=[query], contexts=[])

    assert snapshot.corpus_queries[0].skipped is not None


@pytest.mark.parametrize(
    "missing",
    ["prompt_snapshot", "corpus_queries", "contexts", "evidence_layout"],
)
def test_snapshot_requires_every_audit_artifact(missing):
    fields = {
        "retrieval_query": "q",
        "corpus_queries": [],
        "contexts": [],
        "evidence_layout": "numbered_list",
        "dedup_merged": 0,
        "index_version": "unknown",
        "model_version": "m",
        "prompt_template_id": "stub-v1",
        "prompt_template_version": "1.0.0",
        "rendered_prompt": "",
        "prompt_snapshot": _prompt_snapshot(),
        "template_vars": {},
    }
    del fields[missing]
    with pytest.raises(ValidationError, match=missing):
        PolicyGateSnapshot(**fields)


def test_a_version_one_snapshot_is_not_a_version_two_snapshot():
    # Bundles decided before DR-34 keep the v1 shape; replay never reads it.
    with pytest.raises(ValidationError):
        PolicyGateSnapshot.model_validate(
            {
                "retrieval_query": "q",
                "retrieval_results": [],
                "retrieval_path": "reranked",
                "corpus_version": "c",
            },
            strict=False,
        )


def test_snapshot_is_frozen():
    snapshot = _make()
    with pytest.raises(ValidationError):
        snapshot.rendered_prompt = "tampered"

"""Behavioral tests for the policy gate's ``RetrievedSnippet`` and ``TokenCost``.

Moved with the types from the framework's ``core`` (Phase 1b): a retrieved
snippet carries the retriever's own unbounded score, and both records are
immutable.
"""

import pytest
from pydantic import ValidationError

from policy_rag_gate.models import TokenCost
from policy_rag_gate.models.snippet import RetrievedSnippet


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

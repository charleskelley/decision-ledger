"""Integration tests for PolicyRetriever.

Requires Docker services (PostgreSQL + pgvector, Elasticsearch) with the
policy corpus already loaded via:

    uv run python -m app.retrieval.corpus_loader

Run with: make test-integration
"""

from __future__ import annotations

import psycopg
import pytest
from elasticsearch import Elasticsearch
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from app.retrieval.retriever import PolicyRetriever

pytestmark = pytest.mark.integration

_PG_DSN = (
    "postgresql://account_takeover:account_takeover@localhost:5432/account_takeover"
)
_ES_URL = "http://localhost:9200"
_MODEL_NAME = "all-MiniLM-L6-v2"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def embed_model() -> SentenceTransformer:
    """Load sentence-transformer once per session — model loading is slow."""
    return SentenceTransformer(_MODEL_NAME)


@pytest.fixture
def pg_conn():
    """Open psycopg connection with pgvector adapter registered."""
    with psycopg.connect(_PG_DSN) as conn:
        register_vector(conn)
        yield conn


@pytest.fixture
def es_client() -> Elasticsearch:
    return Elasticsearch(_ES_URL)


@pytest.fixture
def retriever(pg_conn, es_client, embed_model) -> PolicyRetriever:
    return PolicyRetriever(pg_conn, es_client, embed_model)


# ---------------------------------------------------------------------------
# Dense retrieval
# ---------------------------------------------------------------------------


def test_dense_retrieval_returns_relevant_nist_chunks(retriever):
    """Dense search for MFA/AAL2 returns a NIST policy in the top-5 results.

    Requires corpus to be loaded. NIST-SP-800-63B (AAL1/AAL2/AAL3, MFA) and
    NIST-SP-800-53-AC (access control, MFA) are the canonical references for
    this query. Internal MFA policies and FIDO2 docs rank highly as well — the
    check ensures at least one NIST document is retrieved within k=5.
    """
    snippets = retriever.dense_search("MFA requirements AAL2", k=5)

    assert len(snippets) > 0, "Dense search returned no results — is corpus loaded?"

    all_ids = [s.policy_id for s in snippets]
    assert any("NIST" in pid for pid in all_ids), (
        f"Expected a NIST policy in top-5 dense results, got: {all_ids}"
    )


def test_dense_search_respects_k_limit(retriever):
    """Dense search returns at most k results."""
    snippets = retriever.dense_search("authentication policy", k=3)
    assert len(snippets) <= 3


def test_dense_search_applies_jurisdiction_filter(retriever):
    """Jurisdiction filter restricts results to the specified jurisdiction(s)."""
    snippets = retriever.dense_search(
        "authentication policy",
        k=10,
        jurisdictions=["EU_GDPR"],
    )
    for s in snippets:
        assert s.jurisdiction == "EU_GDPR", (
            f"Expected EU_GDPR jurisdiction, got {s.jurisdiction} from {s.policy_id}"
        )


# ---------------------------------------------------------------------------
# Sparse retrieval
# ---------------------------------------------------------------------------


def test_sparse_search_finds_results_for_known_term(retriever):
    """BM25 sparse search returns results for a term present in the corpus."""
    snippets = retriever.sparse_search("credential stuffing velocity rate limit", k=5)
    assert len(snippets) > 0, "Sparse search returned no results — is corpus loaded?"


def test_sparse_search_finds_exact_regulatory_identifier(retriever):
    """keyword_search analyzer enables exact match on regulatory identifiers."""
    snippets = retriever.sparse_search("NIST-SP-800-63B", k=5)
    assert len(snippets) > 0, (
        "Sparse search found no results for NIST-SP-800-63B — "
        "check dual-analyzer mapping on policy-chunks index"
    )


# ---------------------------------------------------------------------------
# Full retrieval pipeline
# ---------------------------------------------------------------------------


def test_retrieve_pipeline_returns_k_results(retriever):
    """Full pipeline (dense → sparse → RRF → rerank stub) returns exactly k snippets."""
    snippets, path = retriever.retrieve("impossible travel geographic block", k=4)

    assert len(snippets) == 4
    assert path == "rrf_only"


def test_retrieve_pipeline_path_is_rrf_only(retriever):
    """retrieval_path is 'rrf_only' while cross-encoder reranking is stubbed."""
    _, path = retriever.retrieve("MFA policy", k=3)
    assert path == "rrf_only"


def test_retrieve_pipeline_snippets_carry_retrieval_path(retriever):
    """Every returned snippet records the retrieval_path for audit."""
    snippets, _ = retriever.retrieve("authentication risk controls", k=5)
    for s in snippets:
        assert s.retrieval_path in ("rrf_only", "reranked"), (
            f"Unexpected retrieval_path: {s.retrieval_path}"
        )

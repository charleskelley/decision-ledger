"""Integration tests for hybrid retrieval over the ATO policy corpus.

Requires Docker services (PostgreSQL with the RAG policy gate's
``policy_rag`` schema, Elasticsearch) with the ATO policy corpus already
loaded into the reference provider's stores, under the corpus id ATO's
binding names (DR-34), via:

    uv run python -m policy_rag_gate.corpus_loader --corpus-id ato-policy \
        --reasoner-id ato-reasoner

(``make build-policy-index-ato-reasoner``). Run with: make test-integration

These are the ATO-specific retrieval assertions (NIST, AAL2, credential
stuffing, EU_GDPR filtering). The policy gate's own retriever contract is
exercised against its fixture corpus in
``gates/policy_rag/tests/integration/test_retriever.py``; this file checks
that the real corpus is indexed and discoverable as ``ato-policy``, the
corpus ATO's registration binds.
"""

from __future__ import annotations

import psycopg
import pytest
from elasticsearch import Elasticsearch
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from app.settings import FrameworkSettings
from ato_reasoner.registry import ATO_CORPUS_ID
from policy_rag_gate.retriever import PolicyRetriever
from policy_rag_gate.settings import PolicyRagGateSettings

pytestmark = pytest.mark.integration

# Read the DSN from FrameworkSettings and the ES URL from the policy gate's
# own settings, so the tests honor whatever the local .env / docker-compose
# stack actually configured.
_PG_DSN = FrameworkSettings().postgres_dsn
_ES_URL = PolicyRagGateSettings().elasticsearch_url
_MODEL_NAME = "all-MiniLM-L6-v2"
_ATO = ATO_CORPUS_ID


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
    snippets = retriever.dense_search("MFA requirements AAL2", k=5, corpus_id=_ATO)

    assert len(snippets) > 0, "Dense search returned no results — is corpus loaded?"

    all_ids = [s.document_id for s in snippets]
    assert any("NIST" in pid for pid in all_ids), (
        f"Expected a NIST policy in top-5 dense results, got: {all_ids}"
    )


def test_dense_search_applies_jurisdiction_filter(retriever):
    """Jurisdiction filter restricts results to the specified jurisdiction(s)."""
    snippets = retriever.dense_search(
        "authentication policy",
        k=10,
        corpus_id=_ATO,
        jurisdictions=["EU_GDPR"],
    )
    assert snippets, "No EU_GDPR chunks found — is the corpus loaded?"
    for s in snippets:
        assert s.jurisdiction == "EU_GDPR", (
            f"Expected EU_GDPR jurisdiction, got {s.jurisdiction} from {s.document_id}"
        )


def test_dense_search_rows_carry_document_titles(retriever):
    """Chunk rows store the document title; snippets cite it, not the id."""
    snippets = retriever.dense_search("MFA requirements AAL2", k=5, corpus_id=_ATO)
    assert snippets, "Dense search returned no results — is corpus loaded?"
    for s in snippets:
        assert s.title, f"empty title on {s.document_id}"
        assert s.title != s.document_id, (
            f"title fell back to the document id for {s.document_id} — "
            "reload the corpus with --wipe so the title column is populated"
        )


# ---------------------------------------------------------------------------
# Sparse retrieval
# ---------------------------------------------------------------------------


def test_sparse_search_finds_results_for_known_term(retriever):
    """BM25 sparse search returns results for a term present in the corpus."""
    snippets = retriever.sparse_search(
        "credential stuffing velocity rate limit", k=5, corpus_id=_ATO
    )
    assert len(snippets) > 0, "Sparse search returned no results — is corpus loaded?"


def test_sparse_search_finds_exact_regulatory_identifier(retriever):
    """keyword_search analyzer enables exact match on regulatory identifiers."""
    snippets = retriever.sparse_search("NIST-SP-800-63B", k=5, corpus_id=_ATO)
    assert len(snippets) > 0, (
        "Sparse search found no results for NIST-SP-800-63B — "
        "check dual-analyzer mapping on policy-chunks index"
    )


def test_sparse_search_corpus_filter_matches_the_hyphenated_corpus_id(retriever):
    """BM25 ``term`` filter on ``corpus_id`` must match the loaded corpus.

    The DR-24 regression, on the DR-34 key: a ``term`` filter on a field
    dynamically mapped as ``text`` tokenizes the hyphenated id
    ``ato-policy`` and matches nothing — sparse retrieval silently returns
    zero hits while the dense leg masks it. With the keyword mapping, the
    real corpus returns hits and a corpus that is not loaded returns none.
    """
    hits = retriever.sparse_search(
        "credential stuffing velocity rate limit", k=5, corpus_id=_ATO
    )
    assert len(hits) > 0, (
        f"Sparse search with corpus_id={_ATO!r} returned nothing — "
        "is corpus_id mapped as keyword on policy-chunks? Re-run "
        "`make build-policy-index-ato-reasoner` (it recreates a stale index)."
    )

    none = retriever.sparse_search(
        "credential stuffing velocity rate limit",
        k=5,
        corpus_id="no-such-corpus",
    )
    assert none == [], f"Corpus filter leaked across corpora: {none}"


# ---------------------------------------------------------------------------
# Full retrieval pipeline
# ---------------------------------------------------------------------------


def test_retrieve_pipeline_returns_k_results(retriever):
    """Full pipeline (dense → sparse → RRF → rerank stub) returns exactly k snippets."""
    snippets, path = retriever.retrieve(
        "impossible travel geographic block", k=4, corpus_id=_ATO
    )

    assert len(snippets) == 4
    assert path == "rrf_only"
    assert all(s.retrieval_path == "rrf_only" for s in snippets)

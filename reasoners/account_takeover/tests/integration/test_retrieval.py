"""Integration tests for hybrid retrieval over the ATO policy corpus.

Requires Docker services (PostgreSQL + pgvector, Elasticsearch) with the
ATO policy corpus already loaded via:

    uv run python -m app.retrieval.corpus_loader --reasoner-id ato-reasoner

(``make build-policy-index``). Run with: make test-integration

These are the ATO-specific retrieval assertions (NIST, AAL2, credential
stuffing, EU_GDPR filtering). The framework's own retriever contract is
exercised against the stub corpus in ``tests/integration/test_retriever.py``;
this file checks that the real corpus is indexed and discoverable under the
``ato-reasoner`` tenant.
"""

from __future__ import annotations

import psycopg
import pytest
from elasticsearch import Elasticsearch
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from app.retrieval.retriever import PolicyRetriever
from app.settings import FrameworkSettings
from ato_reasoner.registry import ATO_REGISTRATION

pytestmark = pytest.mark.integration

# Read the DSN + ES URL from FrameworkSettings so the tests honor whatever
# the local .env / docker-compose stack actually configured.
_FW = FrameworkSettings()
_PG_DSN = _FW.postgres_dsn
_ES_URL = _FW.elasticsearch_url
_MODEL_NAME = "all-MiniLM-L6-v2"
_ATO = ATO_REGISTRATION.reasoner_id


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
    snippets = retriever.dense_search("MFA requirements AAL2", k=5, reasoner_id=_ATO)

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
        reasoner_id=_ATO,
        jurisdictions=["EU_GDPR"],
    )
    assert snippets, "No EU_GDPR chunks found — is the corpus loaded?"
    for s in snippets:
        assert s.jurisdiction == "EU_GDPR", (
            f"Expected EU_GDPR jurisdiction, got {s.jurisdiction} from {s.document_id}"
        )


def test_dense_search_rows_carry_document_titles(retriever):
    """Chunk rows store the document title; snippets cite it, not the id."""
    snippets = retriever.dense_search("MFA requirements AAL2", k=5, reasoner_id=_ATO)
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
        "credential stuffing velocity rate limit", k=5, reasoner_id=_ATO
    )
    assert len(snippets) > 0, "Sparse search returned no results — is corpus loaded?"


def test_sparse_search_finds_exact_regulatory_identifier(retriever):
    """keyword_search analyzer enables exact match on regulatory identifiers."""
    snippets = retriever.sparse_search("NIST-SP-800-63B", k=5, reasoner_id=_ATO)
    assert len(snippets) > 0, (
        "Sparse search found no results for NIST-SP-800-63B — "
        "check dual-analyzer mapping on policy-chunks index"
    )


def test_sparse_search_tenant_filter_matches_hyphenated_reasoner_id(retriever):
    """BM25 ``term`` filter on ``reasoner_id`` must match the loaded tenant.

    Regression for DR-24: before ``reasoner_id`` was mapped as ``keyword``
    the field was dynamically mapped as ``text``, so the hyphenated id
    ``ato-reasoner`` was tokenized and a ``term`` filter matched nothing —
    production sparse retrieval silently returned zero hits while the dense
    leg masked it. With the keyword mapping, the real tenant returns hits
    and an unknown tenant returns none.
    """
    hits = retriever.sparse_search(
        "credential stuffing velocity rate limit", k=5, reasoner_id=_ATO
    )
    assert len(hits) > 0, (
        f"Sparse search with reasoner_id={_ATO!r} returned nothing — "
        "is reasoner_id mapped as keyword on policy-chunks? Re-run "
        "`make build-policy-index` (it recreates a stale index)."
    )

    none = retriever.sparse_search(
        "credential stuffing velocity rate limit",
        k=5,
        reasoner_id="no-such-reasoner",
    )
    assert none == [], f"Tenant filter leaked across reasoners: {none}"


# ---------------------------------------------------------------------------
# Full retrieval pipeline
# ---------------------------------------------------------------------------


def test_retrieve_pipeline_returns_k_results(retriever):
    """Full pipeline (dense → sparse → RRF → rerank stub) returns exactly k snippets."""
    snippets, path = retriever.retrieve(
        "impossible travel geographic block", k=4, reasoner_id=_ATO
    )

    assert len(snippets) == 4
    assert path == "rrf_only"
    assert all(s.retrieval_path == "rrf_only" for s in snippets)

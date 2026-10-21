"""Integration tests for PolicyRetriever against the gate's fixture corpus.

Requires Docker services (PostgreSQL, Elasticsearch). Run with:
make test-integration

The gate's retriever is reasoner-agnostic, so its contract is proved on the
fixture corpus under ``gates/policy_rag/tests/fixtures/corpus`` (two short
Markdown policies, ``REGION_A`` and ``REGION_B``). A session fixture applies
the gate's own DDL (vector extension, ``policy_rag`` schema), then indexes
that corpus under ``reasoner_id="stub-reasoner"`` with the corpus loader's
public helpers — what ``corpus_loader --reasoner-id stub-reasoner
--corpus-dir <fixtures>/corpus`` would write, without the CLI's check that
the ledger registers ``stub-reasoner`` — and deletes those rows and
documents again on teardown, so a shared local stack is left as it was
found.

Covered:

- BM25 ``term`` filter matches the hyphenated tenant id (the DR-24
  regression) and returns nothing for another tenant.
- Exact identifier search via the ``keyword_search`` analyzer.
- Dense search is tenant-scoped, honors ``k``, carries document titles, and
  applies the jurisdiction filter.
- The full pipeline returns ``k`` RRF-fused snippets, each stamped with its
  retrieval path, and an unknown tenant yields nothing from either leg.

The ATO corpus assertions (NIST, AAL2, …) live in
``reasoners/account_takeover/tests/integration/test_retrieval.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from elasticsearch import Elasticsearch
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from app.settings import FrameworkSettings
from policy_rag_gate.corpus_loader import (
    ES_INDEX,
    chunk_document,
    ensure_es_index,
    parse_document,
    write_elasticsearch,
    write_postgres,
)
from policy_rag_gate.retriever import PolicyRetriever
from policy_rag_gate.schema import apply_schema
from policy_rag_gate.settings import PolicyRagGateSettings

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

# Read the DSN from FrameworkSettings (the gate's schema lives in the
# framework's database) and the ES URL from the gate's own settings, so the
# tests honor whatever the local .env / docker-compose stack configured.
_PG_DSN = FrameworkSettings().postgres_dsn
_ES_URL = PolicyRagGateSettings().elasticsearch_url
_MODEL_NAME = "all-MiniLM-L6-v2"

#: The fixture corpus and the tenant it is indexed under.
_CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"
_STUB = "stub-reasoner"
_STUB_DOC_IDS = {"STUB-POLICY-A", "STUB-POLICY-B"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def embed_model() -> SentenceTransformer:
    """Load sentence-transformer once per session — model loading is slow."""
    return SentenceTransformer(_MODEL_NAME)


def _delete_stub_rows(conn: psycopg.Connection[Any], es: Elasticsearch) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM policy_rag.policy_chunks WHERE reasoner_id = %s",
            (_STUB,),
        )
    conn.commit()
    if es.indices.exists(index=ES_INDEX):
        es.delete_by_query(
            index=ES_INDEX, query={"term": {"reasoner_id": _STUB}}, refresh=True
        )


@pytest.fixture(scope="session")
def stub_corpus(embed_model: SentenceTransformer) -> Iterator[int]:
    """Index the fixture corpus under the stub tenant; clean up after.

    Yields the number of chunks written so tests can reason about ``k``.
    """
    es = Elasticsearch(_ES_URL)
    with psycopg.connect(_PG_DSN) as conn:
        apply_schema(conn)
        register_vector(conn)
        ensure_es_index(es)
        _delete_stub_rows(conn, es)

        all_chunks: list[dict[str, Any]] = []
        by_policy: dict[str, list[dict[str, Any]]] = {}
        for path in sorted(_CORPUS_DIR.glob("*.md")):
            doc, body = parse_document(path)
            chunks = chunk_document(doc, body)
            by_policy[doc.policy_id] = chunks
            all_chunks.extend(chunks)

        embeddings = embed_model.encode([c["text"] for c in all_chunks])
        for chunk, embedding in zip(all_chunks, embeddings, strict=True):
            chunk["embedding"] = embedding.tolist()

        for policy_id, chunks in by_policy.items():
            write_postgres(
                conn, chunks, reasoner_id=_STUB, policy_id=policy_id, wipe=False
            )
        conn.commit()
        write_elasticsearch(es, all_chunks, reasoner_id=_STUB)
        es.indices.refresh(index=ES_INDEX)

        yield len(all_chunks)

        _delete_stub_rows(conn, es)
    es.close()


@pytest.fixture
def pg_conn(stub_corpus: int) -> Iterator[psycopg.Connection[Any]]:
    """Open psycopg connection with pgvector adapter registered."""
    with psycopg.connect(_PG_DSN) as conn:
        register_vector(conn)
        yield conn


@pytest.fixture
def es_client(stub_corpus: int) -> Iterator[Elasticsearch]:
    es = Elasticsearch(_ES_URL)
    yield es
    es.close()


@pytest.fixture
def retriever(pg_conn, es_client, embed_model) -> PolicyRetriever:
    return PolicyRetriever(pg_conn, es_client, embed_model)


# ---------------------------------------------------------------------------
# Sparse retrieval — tenant filter on a hyphenated id (DR-24 regression)
# ---------------------------------------------------------------------------


def test_sparse_search_tenant_filter_matches_hyphenated_reasoner_id(retriever):
    """BM25 ``term`` filter on ``reasoner_id`` must match the loaded tenant.

    Before ``reasoner_id`` was mapped as ``keyword`` the field was dynamically
    mapped as ``text``, so a hyphenated id was tokenized and a ``term`` filter
    matched nothing — sparse retrieval silently returned zero hits while the
    dense leg masked it. With the keyword mapping, the real tenant returns
    hits and an unknown tenant returns none.
    """
    hits = retriever.sparse_search("widget calibration review", k=5, reasoner_id=_STUB)
    assert hits, (
        f"Sparse search with reasoner_id={_STUB!r} returned nothing — is "
        "reasoner_id mapped as keyword on policy-chunks? Re-run the corpus "
        "loader (it recreates a stale index)."
    )
    assert {s.document_id for s in hits} <= _STUB_DOC_IDS

    none = retriever.sparse_search(
        "widget calibration review", k=5, reasoner_id="no-such-reasoner"
    )
    assert none == [], f"Tenant filter leaked across reasoners: {none}"


def test_sparse_search_finds_exact_policy_identifier(retriever):
    """The ``keyword_search`` analyzer matches a hyphenated identifier verbatim."""
    hits = retriever.sparse_search("STUB-POLICY-B", k=5, reasoner_id=_STUB)
    assert hits, "no sparse hits for STUB-POLICY-B — check the dual-analyzer mapping"
    assert hits[0].document_id == "STUB-POLICY-B"


# ---------------------------------------------------------------------------
# Dense retrieval
# ---------------------------------------------------------------------------


def test_dense_search_is_scoped_to_the_tenant(retriever):
    snippets = retriever.dense_search(
        "widget calibration threshold", k=5, reasoner_id=_STUB
    )
    assert snippets, "dense search returned nothing for the stub tenant"
    assert {s.document_id for s in snippets} <= _STUB_DOC_IDS

    assert retriever.dense_search("widget", k=5, reasoner_id="no-such-reasoner") == []


def test_dense_search_respects_k_limit(retriever, stub_corpus):
    assert stub_corpus > 3, "stub corpus must yield more than k chunks"
    snippets = retriever.dense_search("widget", k=3, reasoner_id=_STUB)
    assert len(snippets) == 3


def test_dense_search_applies_jurisdiction_filter(retriever):
    snippets = retriever.dense_search(
        "widget inspection", k=10, reasoner_id=_STUB, jurisdictions=["REGION_B"]
    )
    assert snippets, "no REGION_B chunks returned"
    assert {s.jurisdiction for s in snippets} == {"REGION_B"}
    assert {s.document_id for s in snippets} == {"STUB-POLICY-B"}


def test_dense_search_rows_carry_document_titles(retriever):
    """Chunk rows store the document title; snippets cite it, not the id."""
    titles = {
        "STUB-POLICY-A": "Stub Widget Policy A",
        "STUB-POLICY-B": "Stub Widget Guidance B",
    }
    snippets = retriever.dense_search("widget", k=6, reasoner_id=_STUB)
    assert snippets
    for s in snippets:
        assert s.title == titles[s.document_id], (s.document_id, s.title)


# ---------------------------------------------------------------------------
# Full retrieval pipeline
# ---------------------------------------------------------------------------


def test_retrieve_pipeline_returns_k_rrf_fused_results(retriever):
    """Dense → sparse → RRF → (no reranker) returns exactly k stub snippets."""
    snippets, path = retriever.retrieve(
        "thin history calibration review", k=4, reasoner_id=_STUB
    )

    assert len(snippets) == 4
    assert path == "rrf_only"
    assert all(s.retrieval_path == "rrf_only" for s in snippets)
    assert {s.document_id for s in snippets} <= _STUB_DOC_IDS


def test_retrieve_pipeline_with_jurisdiction_filter_spans_both_stores(retriever):
    """The jurisdiction filter is applied to both legs, so RRF output honors it."""
    snippets, _ = retriever.retrieve(
        "widget", k=5, reasoner_id=_STUB, jurisdictions=["REGION_A"]
    )
    assert snippets
    assert {s.jurisdiction for s in snippets} == {"REGION_A"}


def test_retrieve_pipeline_returns_nothing_for_an_unknown_tenant(retriever):
    snippets, path = retriever.retrieve("widget", k=4, reasoner_id="no-such-reasoner")
    assert snippets == []
    assert path == "rrf_only"

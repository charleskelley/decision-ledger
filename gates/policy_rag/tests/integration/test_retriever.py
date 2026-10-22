"""Integration tests for the reference provider's stores, on the fixture corpus.

Requires Docker services (PostgreSQL, Elasticsearch). Run with:
make test-integration

The reference retrieval provider (DR-34) is corpus-agnostic, so its contract
is proved on the fixture corpus under ``gates/policy_rag/tests/fixtures/corpus``
(two short Markdown policies, ``REGION_A`` and ``REGION_B``). A session
fixture applies the gate's own DDL, then loads that corpus twice, under two
corpus ids, with the corpus loader's public helpers — what ``corpus_loader
--corpus-id <id> --corpus-dir <fixtures>/corpus`` writes — and deletes both
corpora again on teardown, so a shared local stack is left as it was found.

Covered:

- The corpus filter: a BM25 ``term`` filter on a hyphenated ``corpus_id``
  matches, and both legs return only the corpus asked, though the two
  corpora hold the same documents.
- Exact identifier search via the ``keyword_search`` analyzer.
- Dense search honors ``k``, carries document titles, and applies the
  jurisdiction filter; the full pipeline returns ``k`` RRF-fused chunks and
  nothing for a corpus that is not loaded.
- Deterministic chunk ids: a reload writes the same ids in both stores and
  no duplicates, and a document removed from the corpus loses its chunks in
  both.
- What the loader records: the corpus's manifest hash and document count,
  each document whole with its front matter, and chunk spans that slice the
  document body.
- ``ReferenceProvider`` against the stores: ``describe`` reports the loaded
  corpus, and ``retrieve`` returns passages under the evidence contract.

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
from psycopg.rows import dict_row
from sentence_transformers import SentenceTransformer

from app.settings import FrameworkSettings
from policy_rag_gate.corpus import CorpusRef
from policy_rag_gate.corpus_loader import (
    ES_INDEX,
    assign_chunk_ids,
    assign_spans,
    chunk_document,
    ensure_es_index,
    manifest_hash,
    read_document,
    write_elasticsearch,
    write_postgres,
)
from policy_rag_gate.models import content_hash
from policy_rag_gate.providers import DocumentNotHeldError
from policy_rag_gate.providers.reference import (
    CorpusNotLoadedError,
    ReferenceProvider,
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

#: The fixture corpus and the two corpus ids it is loaded under.
_CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"
_STUB = "it-stub-policy"
_TWIN = "it-stub-policy-twin"
_STUB_DOC_IDS = {"STUB-POLICY-A", "STUB-POLICY-B"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def embed_model() -> SentenceTransformer:
    """Load sentence-transformer once per session — model loading is slow."""
    return SentenceTransformer(_MODEL_NAME)


def _delete_corpus(
    conn: psycopg.Connection[Any], es: Elasticsearch, corpus: str
) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM policy_rag.corpora WHERE corpus_id = %s", (corpus,))
    conn.commit()
    if es.indices.exists(index=ES_INDEX):
        es.delete_by_query(
            index=ES_INDEX, query={"term": {"corpus_id": corpus}}, refresh=True
        )


def _load(
    conn: psycopg.Connection[Any],
    es: Elasticsearch,
    model: SentenceTransformer,
    corpus: str,
    paths: list[Path],
) -> list[dict[str, Any]]:
    """Load ``paths`` as ``corpus`` exactly as the corpus loader does."""
    documents = [read_document(path, _CORPUS_DIR) for path in paths]
    chunks: list[dict[str, Any]] = []
    for document in documents:
        document_chunks = chunk_document(document.document, document.body)
        assign_chunk_ids(corpus, document_chunks)
        assign_spans(document.body, document_chunks)
        chunks.extend(document_chunks)
    for chunk, embedding in zip(
        chunks, model.encode([c["text"] for c in chunks]), strict=True
    ):
        chunk["embedding"] = embedding.tolist()
    write_postgres(
        conn,
        corpus_id=corpus,
        source_dir=_CORPUS_DIR,
        documents=documents,
        chunks=chunks,
    )
    conn.commit()
    write_elasticsearch(es, chunks, corpus_id=corpus)
    es.indices.refresh(index=ES_INDEX)
    return chunks


@pytest.fixture(scope="session")
def stores(embed_model: SentenceTransformer) -> Iterator[dict[str, Any]]:
    """Load the fixture corpus as two corpora; clean both up after."""
    es = Elasticsearch(_ES_URL)
    paths = sorted(_CORPUS_DIR.glob("*.md"))
    with psycopg.connect(_PG_DSN) as conn:
        apply_schema(conn)
        register_vector(conn)
        ensure_es_index(es)
        for corpus in (_STUB, _TWIN):
            _delete_corpus(conn, es, corpus)
        chunks = _load(conn, es, embed_model, _STUB, paths)
        _load(conn, es, embed_model, _TWIN, paths)

        yield {"conn": conn, "es": es, "paths": paths, "chunks": chunks}

        for corpus in (_STUB, _TWIN):
            _delete_corpus(conn, es, corpus)
    es.close()


@pytest.fixture
def pg_conn(stores: dict[str, Any]) -> Iterator[psycopg.Connection[Any]]:
    """Open an autocommit psycopg connection with the pgvector adapter."""
    with psycopg.connect(_PG_DSN, autocommit=True) as conn:
        register_vector(conn)
        yield conn


@pytest.fixture
def es_client(stores: dict[str, Any]) -> Iterator[Elasticsearch]:
    es = Elasticsearch(_ES_URL)
    yield es
    es.close()


@pytest.fixture
def retriever(pg_conn, es_client, embed_model) -> PolicyRetriever:
    return PolicyRetriever(pg_conn, es_client, embed_model)


def _chunk_ids(conn: psycopg.Connection[Any], corpus: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT chunk_id FROM policy_rag.policy_chunks WHERE corpus_id = %s",
            (corpus,),
        )
        return {str(row[0]) for row in cur.fetchall()}


def _es_ids(es: Elasticsearch, corpus: str) -> set[str]:
    response = es.search(
        index=ES_INDEX, query={"term": {"corpus_id": corpus}}, size=1000
    )
    return {hit["_id"] for hit in response["hits"]["hits"]}


# ---------------------------------------------------------------------------
# The corpus filter, in both stores
# ---------------------------------------------------------------------------


def test_sparse_search_corpus_filter_matches_a_hyphenated_corpus_id(retriever):
    """BM25 ``term`` filter on ``corpus_id`` must match the loaded corpus.

    A ``term`` filter on a field dynamically mapped as ``text`` tokenizes a
    hyphenated id and matches nothing, so the mapping declares ``corpus_id``
    as ``keyword``; a corpus that is not loaded returns nothing.
    """
    hits = retriever.sparse_search("widget calibration review", k=5, corpus_id=_STUB)
    assert hits, (
        f"Sparse search with corpus_id={_STUB!r} returned nothing — is corpus_id "
        "mapped as keyword on policy-chunks? Re-run the corpus loader (it "
        "recreates a stale index)."
    )
    assert {s.document_id for s in hits} <= _STUB_DOC_IDS

    none = retriever.sparse_search(
        "widget calibration review", k=5, corpus_id="no-such-corpus"
    )
    assert none == [], f"Corpus filter leaked across corpora: {none}"


def test_both_legs_return_only_the_corpus_asked(retriever, stores, pg_conn):
    # The twin corpus holds the same documents under its own chunk ids.
    stub_ids = _chunk_ids(pg_conn, _STUB)
    twin_ids = _chunk_ids(pg_conn, _TWIN)
    assert stub_ids
    assert not stub_ids & twin_ids

    dense = retriever.dense_search("widget", k=20, corpus_id=_STUB)
    sparse = retriever.sparse_search("widget", k=20, corpus_id=_STUB)

    assert dense
    assert sparse
    assert {c.chunk_id for c in dense} <= stub_ids
    assert {c.chunk_id for c in sparse} <= stub_ids


def test_sparse_search_finds_exact_policy_identifier(retriever):
    """The ``keyword_search`` analyzer matches a hyphenated identifier verbatim."""
    hits = retriever.sparse_search("STUB-POLICY-B", k=5, corpus_id=_STUB)
    assert hits, "no sparse hits for STUB-POLICY-B — check the dual-analyzer mapping"
    assert hits[0].document_id == "STUB-POLICY-B"


# ---------------------------------------------------------------------------
# Dense retrieval and the full pipeline
# ---------------------------------------------------------------------------


def test_dense_search_respects_k_limit(retriever, stores):
    assert len(stores["chunks"]) > 3, "stub corpus must yield more than k chunks"
    chunks = retriever.dense_search("widget", k=3, corpus_id=_STUB)
    assert len(chunks) == 3


def test_dense_search_applies_jurisdiction_filter(retriever):
    chunks = retriever.dense_search(
        "widget inspection", k=10, corpus_id=_STUB, jurisdictions=["REGION_B"]
    )
    assert chunks, "no REGION_B chunks returned"
    assert {c.jurisdiction for c in chunks} == {"REGION_B"}
    assert {c.document_id for c in chunks} == {"STUB-POLICY-B"}


def test_dense_search_rows_carry_document_titles(retriever):
    """Chunk rows store the document title; chunks cite it, not the id."""
    titles = {
        "STUB-POLICY-A": "Stub Widget Policy A",
        "STUB-POLICY-B": "Stub Widget Guidance B",
    }
    chunks = retriever.dense_search("widget", k=6, corpus_id=_STUB)
    assert chunks
    for c in chunks:
        assert c.title == titles[c.document_id], (c.document_id, c.title)


def test_retrieve_pipeline_returns_k_rrf_fused_results(retriever):
    """Dense → sparse → RRF → (no reranker) returns exactly k stub chunks."""
    chunks, path = retriever.retrieve(
        "thin history calibration review", k=4, corpus_id=_STUB
    )

    assert len(chunks) == 4
    assert path == "rrf_only"
    assert all(c.retrieval_path == "rrf_only" for c in chunks)
    assert {c.document_id for c in chunks} <= _STUB_DOC_IDS


def test_retrieve_pipeline_with_jurisdiction_filter_spans_both_stores(retriever):
    """The jurisdiction filter is applied to both legs, so RRF output honors it."""
    chunks, _ = retriever.retrieve(
        "widget", k=5, corpus_id=_STUB, jurisdictions=["REGION_A"]
    )
    assert chunks
    assert {c.jurisdiction for c in chunks} == {"REGION_A"}


def test_retrieve_pipeline_returns_nothing_for_a_corpus_that_is_not_loaded(
    retriever,
):
    chunks, path = retriever.retrieve("widget", k=4, corpus_id="no-such-corpus")
    assert chunks == []
    assert path == "rrf_only"


# ---------------------------------------------------------------------------
# What the loader records, and reloading
# ---------------------------------------------------------------------------


def test_the_corpus_row_records_its_manifest_and_documents(stores, pg_conn):
    documents = [read_document(path, _CORPUS_DIR) for path in stores["paths"]]
    with pg_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT manifest_hash, document_count, source_dir "
            "FROM policy_rag.corpora WHERE corpus_id = %s",
            (_STUB,),
        )
        corpus = cur.fetchone()
        cur.execute(
            "SELECT document_id, document_version, content_hash, body, metadata, "
            "source_path FROM policy_rag.documents WHERE corpus_id = %s "
            "ORDER BY document_id",
            (_STUB,),
        )
        rows = cur.fetchall()

    assert corpus is not None
    assert corpus["manifest_hash"] == manifest_hash(documents)
    assert corpus["document_count"] == 2
    assert corpus["source_dir"] == str(_CORPUS_DIR)
    assert [row["document_id"] for row in rows] == ["STUB-POLICY-A", "STUB-POLICY-B"]
    for row in rows:
        assert row["content_hash"] == content_hash(row["body"])
        assert row["metadata"]["policy_id"] == row["document_id"]
        assert row["source_path"].endswith(".md")


def test_chunk_spans_slice_the_document_body(pg_conn):
    with pg_conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT c.chunk_text, c.span_start, c.span_end, d.body "
            "FROM policy_rag.policy_chunks c JOIN policy_rag.documents d "
            "ON (d.corpus_id, d.document_id, d.document_version) "
            "= (c.corpus_id, c.policy_id, c.policy_version) "
            "WHERE c.corpus_id = %s",
            (_STUB,),
        )
        rows = cur.fetchall()

    located = [row for row in rows if row["span_start"] is not None]
    assert located, "no chunk was located in its document body"
    for row in located:
        assert row["body"][row["span_start"] : row["span_end"]] == row["chunk_text"]


def test_a_reload_writes_the_same_ids_and_no_duplicates(stores, embed_model, pg_conn):
    conn, es = stores["conn"], stores["es"]
    before_pg, before_es = _chunk_ids(pg_conn, _TWIN), _es_ids(es, _TWIN)

    _load(conn, es, embed_model, _TWIN, stores["paths"])

    assert _chunk_ids(pg_conn, _TWIN) == before_pg
    assert _es_ids(es, _TWIN) == before_es == before_pg


def test_a_document_removed_from_the_corpus_loses_its_chunks(
    stores, embed_model, pg_conn
):
    conn, es = stores["conn"], stores["es"]
    [first, *_] = stores["paths"]

    kept = _load(conn, es, embed_model, _TWIN, [first])

    kept_ids = {c["chunk_id"] for c in kept}
    assert _chunk_ids(pg_conn, _TWIN) == kept_ids
    assert _es_ids(es, _TWIN) == kept_ids
    # Restore the twin for any test that runs after this one.
    _load(conn, es, embed_model, _TWIN, stores["paths"])


# ---------------------------------------------------------------------------
# ReferenceProvider against the stores
# ---------------------------------------------------------------------------


def test_the_reference_provider_describes_and_retrieves_a_loaded_corpus(
    retriever, pg_conn, stores
):
    provider = ReferenceProvider(retriever=retriever, pg_conn=pg_conn)
    corpus = CorpusRef(corpus_id=_STUB, tier=2)

    state = provider.describe(corpus)
    result = provider.retrieve(
        "widget calibration review", corpus, k=3, jurisdictions=None, risk_tier=None
    )

    assert state.document_count == 2
    assert state.provenance_level == "pinned_version"
    assert result.state == state
    assert 1 <= len(result.contexts) <= 3
    chunk_ids = _chunk_ids(pg_conn, _STUB)
    for rank, context in enumerate(result.contexts, 1):
        assert (context.corpus_id, context.tier, context.rank) == (_STUB, 2, rank)
        assert context.content_hash == content_hash(context.text)
        assert context.provider_chunk_id in chunk_ids
        assert context.document_metadata["policy_id"] == context.document_id
    with pytest.raises(CorpusNotLoadedError):
        provider.describe(CorpusRef(corpus_id="no-such-corpus", tier=1))


def test_the_reference_provider_fetches_what_it_retrieved_from_whole(
    retriever, pg_conn, stores
):
    # DR-34 part 2: each passage retrieved is a slice of the document the
    # provider returns whole, from Postgres alone.
    provider = ReferenceProvider(retriever=retriever, pg_conn=pg_conn)
    corpus = CorpusRef(corpus_id=_STUB, tier=1)
    result = provider.retrieve(
        "widget calibration review", corpus, k=3, jurisdictions=None, risk_tier=None
    )
    assert result.contexts

    for context in result.contexts:
        document = provider.fetch_document(corpus, context.document_id, context.version)
        assert (document.document_id, document.version) == (
            context.document_id,
            context.version,
        )
        assert document.content_hash == content_hash(document.text)
        if context.span is not None:
            start, end = context.span
            assert content_hash(document.text[start:end]) == context.content_hash
        else:
            assert context.text in document.text

    first = result.contexts[0]
    with pytest.raises(DocumentNotHeldError):
        provider.fetch_document(corpus, first.document_id, "0.0-never-loaded")
    with pytest.raises(CorpusNotLoadedError):
        provider.fetch_document(
            CorpusRef(corpus_id="no-such-corpus", tier=1), first.document_id, "1.0"
        )

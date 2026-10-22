"""The reference retrieval provider, without its stores (DR-34).

``ReferenceProvider`` puts today's hybrid stack behind the provider seam.
These tests pin, with every store and model faked:

- building (``open_reference_provider``, the ``reference`` plugin): its own
  autocommit Postgres connection, the gate's DDL before the pgvector
  adapters, the Elasticsearch client, the embedding and cross-encoder
  models — and a failed build closes the connection;
- ``describe``: a loaded corpus's manifest hash, document count, and load
  time from ``policy_rag.corpora``, with ``pinned_version`` provenance; a
  corpus not loaded, or a reference naming a resource, is refused;
- ``retrieve``: one corpus-filtered retrieval, each chunk returned under the
  evidence contract with the reference's tier, its rank, its score named
  for the ranking applied, its chunk id, span, and its document's metadata.

The stores themselves are covered by the integration tests
(``tests/integration/test_retriever.py``).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from policy_rag_gate.corpus import CorpusRef
from policy_rag_gate.models import content_hash
from policy_rag_gate.providers import (
    DocumentFetcher,
    DocumentNotHeldError,
    ProviderServices,
)
from policy_rag_gate.providers import reference as reference_module
from policy_rag_gate.providers.reference import (
    PLUGIN,
    REFERENCE_PROVIDER_VERSION,
    CorpusNotLoadedError,
    ReferenceProvider,
    open_reference_provider,
)
from policy_rag_gate.retriever import ScoredChunk
from policy_rag_gate.settings import PolicyRagGateSettings

_LOADED_AT = datetime(2026, 10, 21, 9, 30, tzinfo=UTC)
_BODY_A = "# Stub Widget Policy A\n\n## 2. Calibration Review\n\nHold thin histories.\n"


class _Cursor:
    """A cursor answering the provider's two queries from fixed rows."""

    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any) -> None:
        self._conn.executed.append((" ".join(sql.split()), params))
        if "left join policy_rag.documents" in sql:
            document_id, version, corpus_id = params
            if corpus_id not in self._conn.corpora:
                self._rows = []
                return
            found = [
                row
                for row in self._conn.documents
                if (row["corpus_id"], row["document_id"], row["document_version"])
                == (corpus_id, document_id, version)
            ]
            self._rows = (
                [
                    {
                        "title": found[0]["title"],
                        "body": found[0]["body"],
                        "metadata": found[0]["metadata"],
                    }
                ]
                if found
                else [{"title": None, "body": None, "metadata": None}]
            )
        elif "from policy_rag.corpora" in sql:
            (corpus_id,) = params
            row = self._conn.corpora.get(corpus_id)
            self._rows = [row] if row is not None else []
        elif "from policy_rag.policy_chunks" in sql:
            corpus_id, ids = params
            metadata = {
                (row["corpus_id"], row["document_id"]): row["metadata"]
                for row in self._conn.documents
            }
            self._rows = [
                {
                    "chunk_id": chunk_id,
                    "chunk_text": text,
                    "metadata": metadata[(corpus_id, document_id)],
                }
                for (held_corpus, chunk_id), (document_id, text) in sorted(
                    self._conn.chunks.items()
                )
                if held_corpus == corpus_id and chunk_id in ids
            ]

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _Conn:
    """Postgres as the provider sees it: corpora, documents, and chunks."""

    def __init__(self, chunks: list[ScoredChunk]) -> None:
        self.executed: list[tuple[str, Any]] = []
        # (corpus_id, chunk_id) -> (document_id, chunk_text): by default
        # Postgres holds exactly what retrieval returns.
        self.chunks = {
            ("stub-policy", chunk.chunk_id): (chunk.document_id, chunk.text)
            for chunk in chunks
        }
        self.corpora = {
            "stub-policy": {
                "manifest_hash": "f" * 64,
                "document_count": 2,
                "loaded_at": _LOADED_AT,
            }
        }
        self.documents = [
            {
                "corpus_id": "stub-policy",
                "document_id": "STUB-POLICY-A",
                "document_version": "1.0",
                "title": "Stub Widget Policy A",
                "body": _BODY_A,
                "metadata": {"policy_id": "STUB-POLICY-A", "approver": "risk-office"},
            },
            {
                "corpus_id": "stub-policy",
                "document_id": "STUB-POLICY-B",
                "document_version": "1.0",
                "title": "Stub Widget Policy B",
                "body": "# Stub Widget Policy B\n\nRelease after inspection.\n",
                "metadata": None,
            },
        ]

    def cursor(self, row_factory: Any = None) -> _Cursor:
        del row_factory
        return _Cursor(self)


class _Retriever:
    def __init__(self, chunks: list[ScoredChunk], path: str, *, reranks: bool) -> None:
        self.calls: list[dict[str, Any]] = []
        self._chunks = chunks
        self._path = path
        self.reranks = reranks

    def retrieve(self, query: str, k: int, **filters: Any) -> Any:
        self.calls.append({"query": query, "k": k, **filters})
        return self._chunks, self._path


def _chunk(document_id: str, text: str, *, score: float = 0.9) -> ScoredChunk:
    return ScoredChunk(
        chunk_id=f"chunk-{document_id}",
        document_id=document_id,
        title=f"Policy {document_id}",
        version="1.0",
        jurisdiction="REGION_A",
        section_path="2. Calibration Review",
        text=text,
        relevance_score=score,
        retrieval_path="reranked",
        span=(10, 10 + len(text)),
    )


def _provider(
    chunks: list[ScoredChunk] | None = None,
    *,
    path: str = "reranked",
    reranks: bool = True,
) -> tuple[ReferenceProvider, _Retriever, _Conn]:
    conn = _Conn(chunks or [])
    retriever = _Retriever(chunks or [], path, reranks=reranks)
    provider = ReferenceProvider(retriever=retriever, pg_conn=conn)  # type: ignore[arg-type]
    return provider, retriever, conn


_REF = CorpusRef(corpus_id="stub-policy", tier=2)


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------


def test_a_loaded_corpus_is_described_by_its_manifest_with_pinned_versions():
    provider, _, _ = _provider()

    state = provider.describe(_REF)

    assert state.corpus_id == "stub-policy"
    assert state.provider == "reference"
    assert state.provider_version == REFERENCE_PROVIDER_VERSION
    assert state.state_marker == "f" * 64
    assert state.provenance_level == "pinned_version"
    assert state.document_count == 2
    assert state.updated_at == _LOADED_AT


def test_a_corpus_that_is_not_loaded_cannot_be_described():
    provider, _, _ = _provider()

    with pytest.raises(CorpusNotLoadedError, match=r"--corpus-id ato-polcy"):
        provider.describe(CorpusRef(corpus_id="ato-polcy", tier=1))


def test_the_reference_provider_takes_no_resource():
    provider, _, conn = _provider()

    with pytest.raises(ValueError, match="takes no resource"):
        provider.describe(
            CorpusRef(corpus_id="stub-policy", tier=1, resource="projects/p")
        )
    assert conn.executed == []


# ---------------------------------------------------------------------------
# retrieve
# ---------------------------------------------------------------------------


def test_retrieval_is_filtered_to_the_corpus_and_returned_under_the_contract():
    provider, retriever, _ = _provider(
        [_chunk("STUB-POLICY-A", "Hold thin histories."), _chunk("STUB-POLICY-B", "b")]
    )

    result = provider.retrieve(
        "thin history", _REF, k=3, jurisdictions=["REGION_A"], risk_tier="STANDARD"
    )

    assert retriever.calls == [
        {
            "query": "thin history",
            "k": 3,
            "corpus_id": "stub-policy",
            "jurisdictions": ["REGION_A"],
            "risk_tier": "STANDARD",
        }
    ]
    first, second = result.contexts
    assert (first.corpus_id, first.provider, first.tier) == (
        "stub-policy",
        "reference",
        2,
    )
    assert [c.rank for c in result.contexts] == [1, 2]
    assert first.content_hash == content_hash("Hold thin histories.")
    assert first.provider_chunk_id == "chunk-STUB-POLICY-A"
    assert first.span == (10, 10 + len("Hold thin histories."))
    assert first.provenance_level == "pinned_version"
    assert first.scores == {"rerank": 0.9}
    assert first.document_metadata == {
        "policy_id": "STUB-POLICY-A",
        "approver": "risk-office",
    }
    assert second.document_metadata == {}
    assert result.state.state_marker == "f" * 64
    assert (result.ranking_requested, result.ranking_applied) == (
        "reranked",
        "reranked",
    )


def test_a_reranker_bypass_is_reported_and_scores_are_named_for_it():
    chunk = dataclasses.replace(_chunk("STUB-POLICY-A", "a"), retrieval_path="rrf_only")
    provider, _, _ = _provider([chunk], path="rrf_only")

    result = provider.retrieve("q", _REF, k=5, jurisdictions=None, risk_tier=None)

    assert (result.ranking_requested, result.ranking_applied) == (
        "reranked",
        "rrf_only",
    )
    assert result.contexts[0].scores == {"rrf": 0.9}


def test_a_query_that_finds_nothing_bypassed_nothing():
    # The retriever reports rrf_only for an empty candidate list; with no
    # candidates there was no ranking to bypass, so the snapshot must not
    # read as a latency-budget fallback.
    provider, _, _ = _provider([], path="rrf_only", reranks=True)

    result = provider.retrieve(
        "q", _REF, k=5, jurisdictions=["NOWHERE"], risk_tier=None
    )

    assert result.contexts == ()
    assert (result.ranking_requested, result.ranking_applied) == (
        "reranked",
        "reranked",
    )


def test_a_hit_postgres_does_not_hold_is_dropped_and_the_ranks_close_up():
    # A sparse hit from an Elasticsearch document a failed load left behind:
    # Postgres is authoritative, so it never reaches the prompt.
    stale = _chunk("STUB-POLICY-B", "Superseded text.")
    provider, _, conn = _provider([stale, _chunk("STUB-POLICY-A", "Current text.")])
    del conn.chunks[("stub-policy", stale.chunk_id)]

    result = provider.retrieve("q", _REF, k=5, jurisdictions=None, risk_tier=None)

    assert [(c.document_id, c.rank) for c in result.contexts] == [("STUB-POLICY-A", 1)]


def test_a_hit_whose_text_postgres_holds_differently_is_dropped():
    # The same chunk id with another text: Elasticsearch kept a version of
    # the passage Postgres no longer holds.
    hit = _chunk("STUB-POLICY-A", "Old wording.")
    provider, _, conn = _provider([hit])
    conn.chunks[("stub-policy", hit.chunk_id)] = ("STUB-POLICY-A", "New wording.")

    result = provider.retrieve("q", _REF, k=5, jurisdictions=None, risk_tier=None)

    assert result.contexts == ()


def test_without_a_reranker_the_ranking_requested_is_rrf_only():
    provider, retriever, _ = _provider([], path="rrf_only", reranks=False)

    result = provider.retrieve("q", _REF, k=5, jurisdictions=[], risk_tier=None)

    assert result.ranking_requested == "rrf_only"
    assert result.contexts == ()
    assert retriever.calls[0]["jurisdictions"] is None


def test_retrieving_from_a_corpus_that_is_not_loaded_fails():
    provider, retriever, _ = _provider()

    with pytest.raises(CorpusNotLoadedError):
        provider.retrieve(
            "q",
            CorpusRef(corpus_id="missing", tier=1),
            k=5,
            jurisdictions=None,
            risk_tier=None,
        )
    assert retriever.calls == []


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


@pytest.fixture
def infra(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace every infrastructure constructor the provider uses with a fake."""
    calls: list[str] = []
    pg_conn = MagicMock(name="pg_conn")
    es = MagicMock(name="es")

    def _connect(dsn: str, *, autocommit: bool = False) -> MagicMock:
        calls.append(f"connect:{dsn}:autocommit={autocommit}")
        return pg_conn

    monkeypatch.setattr(reference_module.psycopg, "connect", _connect)
    monkeypatch.setattr(
        reference_module, "apply_schema", lambda _c: calls.append("apply_schema")
    )
    monkeypatch.setattr(
        reference_module, "register_vector", lambda _c: calls.append("register_vector")
    )

    def _es(url: str) -> MagicMock:
        calls.append(f"elasticsearch:{url}")
        return es

    monkeypatch.setattr(reference_module, "Elasticsearch", _es)
    monkeypatch.setattr(reference_module, "SentenceTransformer", lambda n: f"embed:{n}")
    monkeypatch.setattr(reference_module, "CrossEncoder", lambda n: f"cross:{n}")
    return {"calls": calls, "pg_conn": pg_conn, "es": es}


def test_building_opens_its_own_connection_schema_index_client_and_models(infra):
    provider = PLUGIN.build(
        ProviderServices(
            postgres_dsn="postgresql://u:p@db:5433/d",
            settings=PolicyRagGateSettings(elasticsearch_url="http://es.example:9200"),
        )
    )

    # The gate's own DDL runs before the pgvector adapters are registered.
    assert infra["calls"] == [
        "connect:postgresql://u:p@db:5433/d:autocommit=True",
        "apply_schema",
        "register_vector",
        "elasticsearch:http://es.example:9200",
    ]
    retriever = provider.retriever
    assert retriever._pg is infra["pg_conn"]
    assert retriever._es is infra["es"]
    assert retriever._model == "embed:all-MiniLM-L6-v2"
    assert retriever._cross_encoder == "cross:cross-encoder/ms-marco-MiniLM-L-6-v2"


def test_a_failed_build_closes_the_connection_it_opened(infra, monkeypatch):
    def _boom(name: str) -> None:
        raise RuntimeError("model download failed")

    monkeypatch.setattr(reference_module, "SentenceTransformer", _boom)

    with pytest.raises(RuntimeError, match="model download failed"):
        open_reference_provider(postgres_dsn="dsn", elasticsearch_url="http://es")

    infra["pg_conn"].close.assert_called_once()


def test_close_releases_the_index_client_then_the_connection(infra):
    provider = open_reference_provider(
        postgres_dsn="dsn", elasticsearch_url="http://es"
    )
    order: list[str] = []
    infra["es"].close.side_effect = lambda: order.append("es")
    infra["pg_conn"].close.side_effect = lambda: order.append("pg")

    provider.close()

    assert order == ["es", "pg"]


# ---------------------------------------------------------------------------
# fetch_document (DocumentFetcher, DR-34 part 2)
# ---------------------------------------------------------------------------


def test_the_reference_provider_fetches_whole_documents():
    provider, _, _ = _provider()

    assert isinstance(provider, DocumentFetcher)


def test_a_document_version_is_returned_whole_from_postgres():
    provider, _, conn = _provider()

    document = provider.fetch_document(_REF, "STUB-POLICY-A", "1.0")

    assert (document.corpus_id, document.document_id, document.version) == (
        "stub-policy",
        "STUB-POLICY-A",
        "1.0",
    )
    assert document.title == "Stub Widget Policy A"
    assert document.text == _BODY_A
    assert document.content_hash == content_hash(_BODY_A)
    assert document.media_type == "text/markdown"
    assert document.metadata == {
        "policy_id": "STUB-POLICY-A",
        "approver": "risk-office",
    }
    # One statement, keyed by the corpus, the document, and its version.
    [(sql, params)] = conn.executed
    assert "left join policy_rag.documents" in sql
    assert params == ("STUB-POLICY-A", "1.0", "stub-policy")


def test_a_version_the_loaded_corpus_does_not_hold_is_not_held():
    provider, _, _ = _provider()

    with pytest.raises(DocumentNotHeldError, match=r"no version '2\.0'"):
        provider.fetch_document(_REF, "STUB-POLICY-A", "2.0")


def test_a_document_of_a_corpus_that_is_not_loaded_cannot_be_fetched():
    provider, _, _ = _provider()

    with pytest.raises(CorpusNotLoadedError, match="--corpus-id gone"):
        provider.fetch_document(
            CorpusRef(corpus_id="gone", tier=1), "STUB-POLICY-A", "1.0"
        )


@pytest.mark.parametrize(
    ("corpus", "version", "match"),
    [
        (_REF, None, "by version"),
        (
            CorpusRef(corpus_id="stub-policy", tier=1, resource="projects/p"),
            "1.0",
            "takes no resource",
        ),
    ],
)
def test_a_fetch_needs_a_version_and_no_resource(corpus, version, match):
    provider, _, conn = _provider()

    with pytest.raises(ValueError, match=match):
        provider.fetch_document(corpus, "STUB-POLICY-A", version)
    assert conn.executed == []

"""Behavioral tests for the corpus loader (DR-34): one corpus, by corpus id.

No Postgres or Elasticsearch is touched:

- the CLI: ``--corpus-id`` is required and must be a corpus id; the
  documents come from ``--corpus-dir``, or from where the gate reads a
  reasoner's corpus (``--reasoner-id``): its composed plugin's
  ``corpus_dir`` (discovering the installed plugins filtered by
  ``DECISIONLEDGER_REASONERS``), else its ``corpus/`` directory under the
  gate's asset root (DR-33); an explicit ``--corpus-dir`` wins. The loader
  asks no registry: a corpus is loaded before a registration that binds it
  is admitted;
- documents: each keeps its body, its front matter as JSON metadata, and
  its path; two files declaring one document version refuse the load;
- chunk ids are deterministic (``sha256(corpus_id, document_id, version,
  section_path)``), a repeated section path is told apart, and each chunk
  is located in its document's body when it is a slice of it; the corpus's
  manifest hash changes with its content, not with file order;
- the stores: Postgres replaces the corpus (its ``corpora`` row, documents,
  and chunks) in one transaction; Elasticsearch gets every chunk by id and
  loses the corpus's chunks the load did not write; ``ensure_es_index``
  creates a missing index, keeps one whose ``corpus_id`` is mapped as
  ``keyword``, and drops and recreates one keyed the old way (by
  ``reasoner_id``) while warning that every corpus must be reloaded;
- ``main`` applies the gate's own DDL before its first Postgres write, and
  reads the index URL from the gate's own settings.
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter

from core.exceptions import ReasonerPluginError
from core.observation import GateBinding, ReasonerRegistration
from policy_rag_gate import corpus_loader
from policy_rag_gate.corpus_loader import (
    CorpusDocument,
    _ensure_es_index,
    assign_chunk_ids,
    assign_spans,
    build_parser,
    chunk_document,
    chunk_id,
    duplicate_documents,
    manifest_hash,
    read_document,
    resolve_corpus_dir,
    write_elasticsearch,
    write_postgres,
)
from policy_rag_gate.models.corpus import DocumentType, PolicyDocument

if TYPE_CHECKING:
    from pathlib import Path

    from app.plugin import FrameworkServices

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePlugin:
    """Minimal ``ReasonerPlugin`` with a configurable corpus directory."""

    def __init__(self, reasoner_id: str, corpus_dir: Path) -> None:
        self.registration = ReasonerRegistration(
            reasoner_id=reasoner_id,
            reasoner_name=f"{reasoner_id} (fake)",
            gates=(GateBinding(gate_id="policy"),),
        )
        self.router = APIRouter(prefix=f"/api/v1/{reasoner_id}")
        self.prompts_dir = corpus_dir.parent / "prompts"
        self.corpus_dir = corpus_dir

    def build_services(self, framework: FrameworkServices) -> Any:
        del framework
        return MagicMock()


def _fake_es(*, exists: bool, properties: dict[str, Any] | None = None) -> MagicMock:
    """An Elasticsearch client double exposing ``indices.{exists,get_mapping,...}``.

    ``properties`` is the live index's ``mappings.properties``; ``None`` means
    the index has no explicit properties at all.
    """
    es = MagicMock()
    es.indices.exists.return_value = exists
    es.indices.get_mapping.return_value = {
        "policy-chunks": {"mappings": {"properties": properties or {}}}
    }
    return es


class _RecordingLog:
    """Stand-in for the module's structlog logger; records level + event."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str, event: str, **kw: Any) -> None:
        self.records.append((level, event, kw))

    def debug(self, event: str, **kw: Any) -> None:
        self._record("debug", event, **kw)

    def info(self, event: str, **kw: Any) -> None:
        self._record("info", event, **kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._record("warning", event, **kw)

    def error(self, event: str, **kw: Any) -> None:
        self._record("error", event, **kw)


@pytest.fixture
def recording_log(monkeypatch: pytest.MonkeyPatch) -> _RecordingLog:
    """Replace ``corpus_loader.log`` so assertions do not depend on structlog config."""
    fake = _RecordingLog()
    monkeypatch.setattr(corpus_loader, "log", fake)
    return fake


@pytest.fixture
def mapping_path(tmp_path: Path) -> Path:
    """A minimal index definition file standing in for policy_rag_gate/es."""
    path = tmp_path / "policy-chunks.json"
    path.write_text(
        json.dumps(
            {
                "settings": {"number_of_shards": 1},
                "mappings": {
                    "properties": {
                        "corpus_id": {"type": "keyword"},
                        "text": {"type": "text"},
                    }
                },
            }
        )
    )
    return path


# ---------------------------------------------------------------------------
# parse_document
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "front_matter",
    ["title: NIST SP 800-63B: Digital Identity Guidelines", "title: [unclosed"],
    ids=["unquoted-colon", "unclosed-flow-sequence"],
)
def test_parse_document_reports_a_yaml_syntax_error_as_value_error(
    tmp_path: Path, front_matter: str
):
    # main() skips ValueError/KeyError documents with a warning; a raw YAML
    # error would abort the whole load instead.
    path = tmp_path / "broken.md"
    path.write_text(f"---\n{front_matter}\n---\nBody\n", encoding="utf-8")

    with pytest.raises(ValueError, match="malformed YAML frontmatter"):
        corpus_loader.parse_document(path)


# ---------------------------------------------------------------------------
# resolve_corpus_dir
# ---------------------------------------------------------------------------


def test_resolve_corpus_dir_uses_matching_plugins_corpus_dir(tmp_path: Path):
    alpha = _FakePlugin("alpha-reasoner", tmp_path / "alpha" / "corpus")
    beta = _FakePlugin("beta-reasoner", tmp_path / "beta" / "corpus")

    resolved = resolve_corpus_dir("beta-reasoner", plugins=[alpha, beta])

    assert resolved == tmp_path / "beta" / "corpus"


def test_resolve_corpus_dir_discovers_the_composed_plugins_when_none_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Without an injected list, the loader composes plugins as the ledger does.

    ``discover_plugins`` is asked with the ``DECISIONLEDGER_REASONERS``
    allowlist, so a plugin the ledger leaves out is not the corpus source.
    """
    plugin = _FakePlugin("gamma-reasoner", tmp_path / "gamma" / "corpus")
    calls: list[frozenset[str] | None] = []

    def _fake_discover(*, allow: frozenset[str] | None = None) -> list[_FakePlugin]:
        calls.append(allow)
        return [plugin]

    monkeypatch.setattr(corpus_loader, "discover_plugins", _fake_discover)
    monkeypatch.setenv("DECISIONLEDGER_REASONERS", "gamma-reasoner")

    resolved = resolve_corpus_dir("gamma-reasoner")

    assert resolved == tmp_path / "gamma" / "corpus"
    assert calls == [frozenset({"gamma-reasoner"})]


def _root_corpus(root: Path, reasoner_id: str) -> Path:
    corpus = root / reasoner_id / "corpus"
    corpus.mkdir(parents=True)
    return corpus


def test_resolve_corpus_dir_falls_back_to_the_asset_root(tmp_path: Path):
    """A reasoner registered without a plugin ships its corpus under the root."""
    alpha = _FakePlugin("alpha-reasoner", tmp_path / "alpha" / "corpus")
    corpus = _root_corpus(tmp_path / "root", "file-reasoner")

    resolved = resolve_corpus_dir(
        "file-reasoner", plugins=[alpha], assets_root=tmp_path / "root"
    )

    assert resolved == corpus


def test_resolve_corpus_dir_prefers_a_composed_plugin_over_the_asset_root(
    tmp_path: Path,
):
    """The gate serves a composed plugin's reasoner from the plugin's assets."""
    alpha = _FakePlugin("alpha-reasoner", tmp_path / "alpha" / "corpus")
    _root_corpus(tmp_path / "root", "alpha-reasoner")

    resolved = resolve_corpus_dir(
        "alpha-reasoner", plugins=[alpha], assets_root=tmp_path / "root"
    )

    assert resolved == tmp_path / "alpha" / "corpus"


def test_resolve_corpus_dir_refuses_an_id_that_cannot_name_a_directory(
    tmp_path: Path,
):
    _root_corpus(tmp_path, "outside")
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ReasonerPluginError, match="cannot name a directory"):
        resolve_corpus_dir("../outside", plugins=[], assets_root=root)


def test_resolve_corpus_dir_names_the_missing_asset_root_directory(tmp_path: Path):
    with pytest.raises(ReasonerPluginError) as exc_info:
        resolve_corpus_dir("file-reasoner", plugins=[], assets_root=tmp_path)

    assert str(tmp_path / "file-reasoner" / "corpus") in exc_info.value.reason
    assert "--corpus-dir" in exc_info.value.reason


def test_resolve_corpus_dir_explicit_override_wins_without_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``--corpus-dir`` short-circuits plugin discovery entirely."""

    def _should_not_run(**_: Any) -> list[Any]:
        raise AssertionError("discover_plugins must not be called")

    monkeypatch.setattr(corpus_loader, "discover_plugins", _should_not_run)
    override = tmp_path / "elsewhere"

    assert resolve_corpus_dir("whatever-reasoner", override) == override


def test_resolve_corpus_dir_unknown_reasoner_lists_discovered_ids(tmp_path: Path):
    alpha = _FakePlugin("alpha-reasoner", tmp_path / "alpha")
    beta = _FakePlugin("beta-reasoner", tmp_path / "beta")

    with pytest.raises(ReasonerPluginError) as exc_info:
        resolve_corpus_dir("no-such-reasoner", plugins=[alpha, beta])

    err = exc_info.value
    assert err.plugin_name == "no-such-reasoner"
    assert "alpha-reasoner" in err.reason
    assert "beta-reasoner" in err.reason
    assert "--corpus-dir" in err.reason


def test_cli_parser_requires_a_corpus_id_and_keeps_wipe(tmp_path: Path):
    """``--corpus-id`` is required; ``--reasoner-id``, ``--corpus-dir``, ``--wipe``
    are optional."""
    parser = build_parser()

    minimal = parser.parse_args(["--corpus-id", "x-policy", "--reasoner-id", "x"])
    assert minimal.corpus_id == "x-policy"
    assert minimal.corpus_dir is None
    assert minimal.wipe is False

    full = parser.parse_args(
        ["--corpus-id", "x-policy", "--corpus-dir", str(tmp_path), "--wipe"]
    )
    assert full.reasoner_id is None
    assert full.corpus_dir == tmp_path
    assert full.wipe is True

    with pytest.raises(SystemExit):
        parser.parse_args(["--reasoner-id", "x-reasoner"])


@pytest.mark.parametrize(
    "argv",
    [
        ["--corpus-id", "x-policy"],  # nowhere to read the documents from
        ["--corpus-id", "../escape", "--corpus-dir", "corpus"],
        ["--corpus-id", "has space", "--corpus-dir", "corpus"],
    ],
    ids=["no-source", "path-id", "space-id"],
)
def test_main_refuses_arguments_it_cannot_load(argv: list[str]):
    with pytest.raises(SystemExit) as exc_info:
        corpus_loader.main(argv)
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# _ensure_es_index
# ---------------------------------------------------------------------------


def test_ensure_es_index_creates_missing_index_from_mapping_file(
    mapping_path: Path,
):
    es = _fake_es(exists=False)

    outcome = _ensure_es_index(es, mapping_path=mapping_path)

    assert outcome == "created"
    es.indices.delete.assert_not_called()
    kwargs = es.indices.create.call_args.kwargs
    assert kwargs["index"] == "policy-chunks"
    assert kwargs["mappings"]["properties"]["corpus_id"] == {"type": "keyword"}


def test_ensure_es_index_keeps_index_whose_corpus_id_is_keyword(
    mapping_path: Path,
):
    es = _fake_es(
        exists=True,
        properties={"corpus_id": {"type": "keyword"}, "text": {"type": "text"}},
    )

    outcome = _ensure_es_index(es, mapping_path=mapping_path)

    assert outcome == "kept"
    es.indices.delete.assert_not_called()
    es.indices.create.assert_not_called()


@pytest.mark.parametrize(
    "stale_properties",
    [
        pytest.param(
            {"reasoner_id": {"type": "keyword"}, "text": {"type": "text"}},
            id="keyed-by-reasoner-before-dr34",
        ),
        pytest.param({"text": {"type": "text"}}, id="corpus_id-missing"),
        pytest.param(
            {
                "corpus_id": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword"}},
                }
            },
            id="corpus_id-dynamically-mapped-as-text",
        ),
    ],
)
def test_ensure_es_index_recreates_index_with_stale_corpus_id_mapping(
    mapping_path: Path,
    stale_properties: dict[str, Any],
    recording_log: _RecordingLog,
):
    """A ``term`` filter on a missing or text-mapped corpus_id matches nothing.

    The loader must drop and recreate the index and warn loudly that every
    corpus has to be reloaded.
    """
    es = _fake_es(exists=True, properties=stale_properties)

    outcome = _ensure_es_index(es, mapping_path=mapping_path)

    assert outcome == "recreated"
    es.indices.delete.assert_called_once_with(index="policy-chunks")
    kwargs = es.indices.create.call_args.kwargs
    assert kwargs["mappings"]["properties"]["corpus_id"] == {"type": "keyword"}

    warnings = [r for r in recording_log.records if r[0] == "warning"]
    assert [event for _, event, _ in warnings] == ["es.index_mapping_stale"]
    rendered = " ".join(str(v) for _, _, kw in warnings for v in kw.values())
    assert "every corpus must be reloaded" in rendered


def test_ensure_es_index_does_not_warn_when_mapping_is_current(
    mapping_path: Path, recording_log: _RecordingLog
):
    es = _fake_es(exists=True, properties={"corpus_id": {"type": "keyword"}})

    _ensure_es_index(es, mapping_path=mapping_path)

    assert not [r for r in recording_log.records if r[0] == "warning"]


def test_ensure_es_index_reads_response_body_from_client_response_objects(
    mapping_path: Path,
):
    """The real client returns a response object whose ``.body`` is the dict."""

    class _Response:
        body: ClassVar[dict[str, Any]] = {
            "policy-chunks": {
                "mappings": {"properties": {"corpus_id": {"type": "keyword"}}}
            }
        }

    es = MagicMock()
    es.indices.exists.return_value = True
    es.indices.get_mapping.return_value = _Response()

    assert _ensure_es_index(es, mapping_path=mapping_path) == "kept"
    es.indices.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Documents, chunk ids, spans, and the manifest
# ---------------------------------------------------------------------------


def _document(policy_id: str = "DOC-A", version: str = "1.0") -> PolicyDocument:
    return PolicyDocument(
        policy_id=policy_id,
        title=f"Policy {policy_id}",
        version=version,
        jurisdiction="REGION_A",
        effective_date=date(2024, 1, 1),
        supersedes=None,
        risk_tier=None,
        document_type=DocumentType.INTERNAL_POLICY,
    )


def _corpus_document(
    policy_id: str = "DOC-A", version: str = "1.0", body: str = "Body."
) -> CorpusDocument:
    return CorpusDocument(
        document=_document(policy_id, version),
        body=body,
        metadata={"policy_id": policy_id},
        source_path=f"{policy_id}.md",
    )


def test_a_document_keeps_its_body_its_front_matter_and_its_path(
    fixture_reasoner,
):
    corpus_dir = fixture_reasoner.corpus_dir
    parsed = read_document(corpus_dir / "stub-policy-a.md", corpus_dir)

    assert parsed.document.policy_id == "STUB-POLICY-A"
    assert parsed.source_path == "stub-policy-a.md"
    assert parsed.body.startswith("# Stub Widget Policy A")
    # Front matter as JSON: the date is an ISO string.
    assert parsed.metadata["effective_date"] == "2024-01-01"
    assert parsed.metadata["policy_id"] == "STUB-POLICY-A"
    json.dumps(parsed.metadata)


def test_front_matter_json_cannot_carry_is_refused_naming_the_file(
    fixture_reasoner, tmp_path: Path
):
    source = (fixture_reasoner.corpus_dir / "stub-policy-a.md").read_text(
        encoding="utf-8"
    )
    path = tmp_path / "odd.md"
    path.write_text(source.replace("---\n", "---\nweight: .nan\n", 1), encoding="utf-8")

    with pytest.raises(ValueError, match=r"odd\.md: front matter is not JSON-safe"):
        read_document(path, tmp_path)


def test_a_chunk_id_is_the_same_for_the_same_chunk_and_differs_per_corpus():
    first = chunk_id("ato-policy", "DOC-A", "1.0", "3.2 — Scope")

    assert first == chunk_id("ato-policy", "DOC-A", "1.0", "3.2 — Scope")
    assert len(first) == 64
    assert first != chunk_id("other-policy", "DOC-A", "1.0", "3.2 — Scope")
    assert first != chunk_id("ato-policy", "DOC-A", "1.1", "3.2 — Scope")
    assert first != chunk_id("ato-policy", "DOC-A", "1.0", "3.3 — Scope")


def test_fields_cannot_run_together_into_one_chunk_id():
    assert chunk_id("ab", "c", "1", "s") != chunk_id("a", "bc", "1", "s")


def test_a_repeated_section_path_is_told_apart_and_the_first_keeps_its_id():
    chunks = [
        {"policy_id": "DOC-A", "version": "1.0", "section_path": "Overview"},
        {"policy_id": "DOC-A", "version": "1.0", "section_path": "Overview"},
        {"policy_id": "DOC-A", "version": "1.0", "section_path": "Scope"},
    ]

    assign_chunk_ids("c", chunks)

    ids = [chunk["chunk_id"] for chunk in chunks]
    assert len(set(ids)) == 3
    assert ids[0] == chunk_id("c", "DOC-A", "1.0", "Overview")


def test_reloading_the_fixture_corpus_yields_the_same_chunk_ids(fixture_reasoner):
    corpus_dir = fixture_reasoner.corpus_dir

    def _ids() -> list[str]:
        parsed = read_document(corpus_dir / "stub-policy-a.md", corpus_dir)
        chunks = chunk_document(parsed.document, parsed.body)
        assign_chunk_ids("stub-policy", chunks)
        return [chunk["chunk_id"] for chunk in chunks]

    assert _ids() == _ids()


def test_a_chunk_that_is_a_slice_of_its_body_is_located_in_it():
    body = "# Title\n\n## 1. Scope\n\nWidgets are held.\n\n## 2. Other\n\nX."
    chunks = [{"text": "Widgets are held."}, {"text": "merged\n\nnot a slice"}]

    assign_spans(body, chunks)

    start = body.index("Widgets are held.")
    assert chunks[0]["span"] == (start, start + len("Widgets are held."))
    assert chunks[1]["span"] is None


def test_text_a_document_repeats_is_located_where_each_chunk_has_it():
    body = "## 1. Scope\n\nSame paragraph.\n\n## 2. Exceptions\n\nSame paragraph."
    chunks = [
        {"text": "## 1. Scope"},
        {"text": "Same paragraph."},
        {"text": "## 2. Exceptions"},
        {"text": "Same paragraph."},
    ]

    assign_spans(body, chunks)

    first, second = body.index("Same paragraph."), body.rindex("Same paragraph.")
    assert first != second
    assert chunks[1]["span"] == (first, first + len("Same paragraph."))
    assert chunks[3]["span"] == (second, second + len("Same paragraph."))


def test_the_fixture_corpus_chunks_are_slices_of_their_bodies(fixture_reasoner):
    corpus_dir = fixture_reasoner.corpus_dir
    parsed = read_document(corpus_dir / "stub-policy-a.md", corpus_dir)
    chunks = chunk_document(parsed.document, parsed.body)

    assign_spans(parsed.body, chunks)

    for chunk in chunks:
        if chunk["span"] is not None:
            start, end = chunk["span"]
            assert parsed.body[start:end] == chunk["text"]


def test_the_manifest_changes_with_content_not_with_file_order():
    a, b = _corpus_document("DOC-A", body="a"), _corpus_document("DOC-B", body="b")

    assert manifest_hash([a, b]) == manifest_hash([b, a])
    assert manifest_hash([a, b]) != manifest_hash([a])
    assert manifest_hash([a, b]) != manifest_hash(
        [a, _corpus_document("DOC-B", body="b, edited")]
    )
    assert manifest_hash([a, b]) != manifest_hash(
        [a, _corpus_document("DOC-B", version="2.0", body="b")]
    )


def test_the_manifest_changes_when_front_matter_is_retagged_in_place():
    # Same document, version, and body; the owner retags its jurisdiction
    # (which retrieval filters on) without a new version.
    before = _corpus_document("DOC-A", body="a")
    after = CorpusDocument(
        document=before.document,
        body=before.body,
        metadata={**before.metadata, "jurisdiction": "REGION_B"},
        source_path=before.source_path,
    )

    assert manifest_hash([before]) != manifest_hash([after])
    # Key order in the front matter does not matter.
    reordered = CorpusDocument(
        document=before.document,
        body=before.body,
        metadata=dict(reversed(list({**before.metadata, "x": 1}.items()))),
        source_path=before.source_path,
    )
    with_x = CorpusDocument(
        document=before.document,
        body=before.body,
        metadata={**before.metadata, "x": 1},
        source_path=before.source_path,
    )
    assert manifest_hash([reordered]) == manifest_hash([with_x])


def test_two_files_declaring_one_document_version_are_reported():
    documents = [
        _corpus_document("DOC-A"),
        _corpus_document("DOC-A"),
        _corpus_document("DOC-A", version="2.0"),
    ]

    assert duplicate_documents(documents) == ["DOC-A@1.0"]


# ---------------------------------------------------------------------------
# The stores: a load replaces the corpus
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, calls: list[tuple[str, Any]]) -> None:
        self._calls = calls

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._calls.append(("execute", " ".join(str(sql).split()), params))

    def executemany(self, sql: str, rows: list[Any]) -> None:
        self._calls.append(("executemany", " ".join(str(sql).split()), rows))


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.calls)


def _chunk(document: CorpusDocument, section: str, text: str) -> dict[str, Any]:
    chunk = {
        "policy_id": document.document.policy_id,
        "version": document.document.version,
        "title": document.document.title,
        "jurisdiction": "REGION_A",
        "risk_tier": None,
        "section_path": section,
        "text": text,
        "span": (0, len(text)),
        "embedding": [0.0, 1.0],
    }
    assign_chunk_ids("stub-policy", [chunk])
    return chunk


def test_postgres_replaces_the_corpus_row_documents_and_chunks(tmp_path: Path):
    conn = _Conn()
    document = _corpus_document("DOC-A", body="Widgets are held.")
    chunk = _chunk(document, "1. Scope", "Widgets are held.")

    write_postgres(
        conn,  # type: ignore[arg-type]
        corpus_id="stub-policy",
        source_dir=tmp_path,
        documents=[document],
        chunks=[chunk],
    )

    statements = [(kind, sql.split()[0:3]) for kind, sql, _ in conn.calls]
    assert statements == [
        ("execute", ["insert", "into", "policy_rag.corpora"]),
        ("execute", ["DELETE", "FROM", "policy_rag.documents"]),
        ("executemany", ["insert", "into", "policy_rag.documents"]),
        ("executemany", ["insert", "into", "policy_rag.policy_chunks"]),
    ]
    corpus_row = conn.calls[0][2]
    assert corpus_row == ("stub-policy", manifest_hash([document]), 1, str(tmp_path))
    ((doc_row),) = conn.calls[2][2]
    assert doc_row[:6] == (
        "stub-policy",
        "DOC-A",
        "1.0",
        "Policy DOC-A",
        document.content_hash,
        "Widgets are held.",
    )
    ((chunk_row),) = conn.calls[3][2]
    assert chunk_row[0] == chunk["chunk_id"]
    assert chunk_row[1] == "stub-policy"
    assert chunk_row[9:11] == (0, len("Widgets are held."))


def test_elasticsearch_writes_every_chunk_by_id_then_drops_the_rest_of_the_corpus(
    monkeypatch: pytest.MonkeyPatch,
):
    bulked: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        corpus_loader, "bulk", lambda _es, actions: bulked.append(actions)
    )
    es = MagicMock()
    es.delete_by_query.return_value = {"deleted": 2}
    document = _corpus_document("DOC-A")
    chunks = [_chunk(document, "1", "one"), _chunk(document, "2", "two")]

    stale = write_elasticsearch(es, chunks, corpus_id="stub-policy")

    (actions,) = bulked
    assert [a["_id"] for a in actions] == [c["chunk_id"] for c in chunks]
    assert actions[0]["_source"]["corpus_id"] == "stub-policy"
    assert actions[0]["_source"]["span_start"] == 0
    (delete,) = es.delete_by_query.call_args_list
    query = delete.kwargs["query"]["bool"]
    assert query["filter"] == [{"term": {"corpus_id": "stub-policy"}}]
    assert query["must_not"] == [{"ids": {"values": [c["chunk_id"] for c in chunks]}}]
    assert stale == 2


def test_wipe_deletes_the_corpus_from_elasticsearch_before_writing(
    monkeypatch: pytest.MonkeyPatch,
):
    order: list[str] = []
    monkeypatch.setattr(
        corpus_loader, "bulk", lambda _es, _actions: order.append("bulk")
    )
    es = MagicMock()
    es.delete_by_query.side_effect = lambda **kw: (
        order.append("delete-stale" if "bool" in kw["query"] else "wipe")
        or {"deleted": 0}
    )

    write_elasticsearch(
        es, [_chunk(_corpus_document(), "1", "one")], corpus_id="c", wipe=True
    )

    assert order == ["wipe", "bulk", "delete-stale"]


# ---------------------------------------------------------------------------
# main(): the gate's own schema exists before the first Postgres write
# ---------------------------------------------------------------------------


def _patch_stores(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    class _PgConn:
        def __init__(self, *, autocommit: bool = False) -> None:
            self._locks = autocommit

        def __enter__(self) -> _PgConn:
            return self

        def __exit__(self, *exc: object) -> None:
            if self._locks:
                calls.append("unlock")

        def execute(self, sql: str, params: Any) -> None:
            assert "pg_advisory_lock" in sql
            calls.append(f"lock:{params[0]}")

        def commit(self) -> None:
            calls.append("commit")

        def cursor(self) -> MagicMock:
            return MagicMock()

    class _Model:
        def encode(self, texts: list[str], **_: Any) -> list[Any]:
            return [MagicMock(tolist=lambda: [0.0] * 384) for _ in texts]

    def _elasticsearch(url: str) -> MagicMock:
        calls.append(f"elasticsearch:{url}")
        return MagicMock()

    def _write_postgres(_conn: Any, **kwargs: Any) -> None:
        calls.append(f"write_postgres:{kwargs['corpus_id']}:{len(kwargs['documents'])}")

    def _write_es(_es: Any, chunks: Any, **kwargs: Any) -> int:
        calls.append(f"write_elasticsearch:{kwargs['corpus_id']}")
        return 0

    monkeypatch.setattr(
        corpus_loader.psycopg, "connect", lambda _dsn, **kwargs: _PgConn(**kwargs)
    )
    monkeypatch.setattr(
        corpus_loader, "register_vector", lambda _c: calls.append("register_vector")
    )
    monkeypatch.setattr(corpus_loader, "SentenceTransformer", lambda _n: _Model())
    monkeypatch.setattr(corpus_loader, "Elasticsearch", _elasticsearch)
    monkeypatch.setattr(corpus_loader, "ensure_es_index", lambda _es: None)
    monkeypatch.setattr(corpus_loader, "write_elasticsearch", _write_es)
    monkeypatch.setattr(
        corpus_loader, "apply_schema", lambda _conn: calls.append("apply_schema")
    )
    monkeypatch.setattr(corpus_loader, "write_postgres", _write_postgres)
    monkeypatch.setenv("POSTGRES_PASSWORD", "x")
    monkeypatch.setenv("POLICY_RAG_GATE_ELASTICSEARCH_URL", "http://es.example:9200")


def test_main_applies_the_gates_schema_before_writing_postgres(
    monkeypatch: pytest.MonkeyPatch, fixture_reasoner
) -> None:
    """A fresh database must get the gate's schema before the first insert.

    The loader is often the first process to touch the gate's stores, so it
    applies the gate's idempotent DDL (vector extension, ``policy_rag``
    schema, corpora, documents, and chunk tables) before registering the
    pgvector adapters and writing — the framework's DDL creates none of it.
    """
    calls: list[str] = []
    _patch_stores(monkeypatch, calls)
    monkeypatch.setattr(corpus_loader, "discover_plugins", lambda **_: [])

    corpus_loader.main(
        [
            "--corpus-id",
            "stub-policy",
            "--corpus-dir",
            str(fixture_reasoner.corpus_dir),
        ]
    )

    assert calls.index("apply_schema") < calls.index("register_vector")
    assert calls.index("register_vector") < calls.index("write_postgres:stub-policy:2")
    assert calls.index("write_postgres:stub-policy:2") < calls.index("commit")
    assert "elasticsearch:http://es.example:9200" in calls
    # One load of a corpus at a time: the lock is held across both stores.
    assert calls[0] == "lock:policy_rag.corpus:stub-policy"
    assert calls[-2:] == ["write_elasticsearch:stub-policy", "unlock"]


def test_main_loads_a_reasoners_corpus_from_the_asset_root_before_it_is_registered(
    monkeypatch: pytest.MonkeyPatch, fixture_reasoner, tmp_path: Path
) -> None:
    """The loader asks no registry: a corpus is loaded before a binding names it."""
    import shutil

    shutil.copytree(fixture_reasoner.corpus_dir, tmp_path / "new-reasoner" / "corpus")
    calls: list[str] = []
    _patch_stores(monkeypatch, calls)
    monkeypatch.setattr(corpus_loader, "discover_plugins", lambda **_: [])
    monkeypatch.setenv("POLICY_RAG_GATE_ASSETS_ROOT", str(tmp_path))

    corpus_loader.main(["--corpus-id", "new-policy", "--reasoner-id", "new-reasoner"])

    assert "write_postgres:new-policy:2" in calls
    assert not hasattr(corpus_loader, "registry_refusal")


def test_main_refuses_a_corpus_with_two_files_declaring_one_document(
    monkeypatch: pytest.MonkeyPatch, fixture_reasoner, tmp_path: Path
) -> None:
    import shutil

    corpus = tmp_path / "corpus"
    shutil.copytree(fixture_reasoner.corpus_dir, corpus)
    shutil.copy(corpus / "stub-policy-a.md", corpus / "copy-of-a.md")
    calls: list[str] = []
    _patch_stores(monkeypatch, calls)

    with pytest.raises(SystemExit) as exc_info:
        corpus_loader.main(["--corpus-id", "c", "--corpus-dir", str(corpus)])

    assert exc_info.value.code == 1
    assert calls == []


def test_main_refuses_a_corpus_one_file_of_which_does_not_parse(
    monkeypatch: pytest.MonkeyPatch, fixture_reasoner, tmp_path: Path
) -> None:
    # A load replaces the corpus, so loading the rest would drop that
    # document from both stores and still exit 0.
    import shutil

    corpus = tmp_path / "corpus"
    shutil.copytree(fixture_reasoner.corpus_dir, corpus)
    (corpus / "broken.md").write_text(
        "---\ntitle: a: b\n---\n# Broken\n", encoding="utf-8"
    )
    calls: list[str] = []
    _patch_stores(monkeypatch, calls)

    with pytest.raises(SystemExit) as exc_info:
        corpus_loader.main(["--corpus-id", "c", "--corpus-dir", str(corpus)])

    assert exc_info.value.code == 1
    assert calls == []


def test_main_refuses_a_corpus_no_document_of_which_parses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "no-front-matter.md").write_text("# Policy\n", encoding="utf-8")
    calls: list[str] = []
    _patch_stores(monkeypatch, calls)

    with pytest.raises(SystemExit) as exc_info:
        corpus_loader.main(["--corpus-id", "c", "--corpus-dir", str(corpus)])

    assert exc_info.value.code == 1
    assert calls == []

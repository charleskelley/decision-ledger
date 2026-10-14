"""Behavioral tests for the corpus loader's plugin resolution and index guard.

No Postgres or Elasticsearch is touched:

- ``resolve_corpus_dir`` finds the installed plugin whose
  ``registration.reasoner_id`` matches ``--reasoner-id`` and returns its
  ``corpus_dir``; an explicit ``--corpus-dir`` wins; an unknown id raises
  ``ReasonerPluginError`` naming every discovered id.
- ``_ensure_es_index`` creates a missing index, keeps one whose
  ``reasoner_id`` is mapped as ``keyword``, and drops and recreates one whose
  mapping lacks that field (the DR-24 tenant-filter regression) while
  warning that every corpus must be reloaded.
- ``main`` applies the gate's own DDL before its first Postgres write, and
  reads the index URL from the gate's own settings.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import MagicMock

import pytest
from fastapi import APIRouter

from core.exceptions import ReasonerPluginError
from core.observation import ReasonerRegistration
from policy_rag_gate import corpus_loader
from policy_rag_gate.corpus_loader import (
    _ensure_es_index,
    build_parser,
    resolve_corpus_dir,
)

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
            allowed_gate_ids=frozenset({"policy"}),
        )
        self.router = APIRouter(prefix=f"/api/v1/{reasoner_id}")
        self.prompts_dir = corpus_dir.parent / "prompts"
        self.corpus_dir = corpus_dir
        self.raw_event_factory = dict

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
                        "reasoner_id": {"type": "keyword"},
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


def test_resolve_corpus_dir_discovers_installed_plugins_when_none_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Without an injected list, the loader asks ``discover_plugins()``."""
    plugin = _FakePlugin("gamma-reasoner", tmp_path / "gamma" / "corpus")
    calls: list[int] = []

    def _fake_discover() -> list[_FakePlugin]:
        calls.append(1)
        return [plugin]

    monkeypatch.setattr(corpus_loader, "discover_plugins", _fake_discover)

    resolved = resolve_corpus_dir("gamma-reasoner")

    assert resolved == tmp_path / "gamma" / "corpus"
    assert calls == [1]


def test_resolve_corpus_dir_explicit_override_wins_without_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``--corpus-dir`` short-circuits plugin discovery entirely."""

    def _should_not_run() -> list[Any]:
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


def test_cli_parser_accepts_optional_corpus_dir_and_keeps_wipe(tmp_path: Path):
    """``--reasoner-id`` is required; ``--corpus-dir`` and ``--wipe`` are optional."""
    parser = build_parser()

    minimal = parser.parse_args(["--reasoner-id", "x-reasoner"])
    assert minimal.corpus_dir is None
    assert minimal.wipe is False

    full = parser.parse_args(
        ["--reasoner-id", "x-reasoner", "--corpus-dir", str(tmp_path), "--wipe"]
    )
    assert full.corpus_dir == tmp_path
    assert full.wipe is True

    with pytest.raises(SystemExit):
        parser.parse_args(["--corpus-dir", str(tmp_path)])


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
    assert kwargs["mappings"]["properties"]["reasoner_id"] == {"type": "keyword"}


def test_ensure_es_index_keeps_index_whose_reasoner_id_is_keyword(
    mapping_path: Path,
):
    es = _fake_es(
        exists=True,
        properties={"reasoner_id": {"type": "keyword"}, "text": {"type": "text"}},
    )

    outcome = _ensure_es_index(es, mapping_path=mapping_path)

    assert outcome == "kept"
    es.indices.delete.assert_not_called()
    es.indices.create.assert_not_called()


@pytest.mark.parametrize(
    "stale_properties",
    [
        pytest.param({"text": {"type": "text"}}, id="reasoner_id-missing"),
        pytest.param(
            {
                "reasoner_id": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword"}},
                }
            },
            id="reasoner_id-dynamically-mapped-as-text",
        ),
    ],
)
def test_ensure_es_index_recreates_index_with_stale_reasoner_id_mapping(
    mapping_path: Path,
    stale_properties: dict[str, Any],
    recording_log: _RecordingLog,
):
    """A ``term`` filter on a text-mapped reasoner_id matches nothing (DR-24).

    The loader must drop and recreate the index and warn loudly that every
    reasoner's corpus has to be reloaded.
    """
    es = _fake_es(exists=True, properties=stale_properties)

    outcome = _ensure_es_index(es, mapping_path=mapping_path)

    assert outcome == "recreated"
    es.indices.delete.assert_called_once_with(index="policy-chunks")
    kwargs = es.indices.create.call_args.kwargs
    assert kwargs["mappings"]["properties"]["reasoner_id"] == {"type": "keyword"}

    warnings = [r for r in recording_log.records if r[0] == "warning"]
    assert [event for _, event, _ in warnings] == ["es.index_mapping_stale"]
    rendered = " ".join(str(v) for _, _, kw in warnings for v in kw.values())
    assert "every reasoner's corpus must be reloaded" in rendered


def test_ensure_es_index_does_not_warn_when_mapping_is_current(
    mapping_path: Path, recording_log: _RecordingLog
):
    es = _fake_es(exists=True, properties={"reasoner_id": {"type": "keyword"}})

    _ensure_es_index(es, mapping_path=mapping_path)

    assert not [r for r in recording_log.records if r[0] == "warning"]


def test_ensure_es_index_reads_response_body_from_client_response_objects(
    mapping_path: Path,
):
    """The real client returns a response object whose ``.body`` is the dict."""

    class _Response:
        body: ClassVar[dict[str, Any]] = {
            "policy-chunks": {
                "mappings": {"properties": {"reasoner_id": {"type": "keyword"}}}
            }
        }

    es = MagicMock()
    es.indices.exists.return_value = True
    es.indices.get_mapping.return_value = _Response()

    assert _ensure_es_index(es, mapping_path=mapping_path) == "kept"
    es.indices.delete.assert_not_called()


# ---------------------------------------------------------------------------
# main(): the gate's own schema exists before the first Postgres write
# ---------------------------------------------------------------------------


def test_main_applies_the_gates_schema_before_writing_postgres(
    monkeypatch: pytest.MonkeyPatch, fixture_reasoner
) -> None:
    """A fresh database must get the gate's schema before the first insert.

    The loader is often the first process to touch the gate's stores, so it
    applies the gate's idempotent DDL (vector extension, ``policy_rag``
    schema, chunk table) before registering the pgvector adapters and
    writing — the framework's DDL creates none of it.
    """
    corpus_dir = fixture_reasoner.corpus_dir
    calls: list[str] = []

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

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

    monkeypatch.setattr(corpus_loader.psycopg, "connect", lambda _dsn: _Conn())
    monkeypatch.setattr(
        corpus_loader, "register_vector", lambda _c: calls.append("register_vector")
    )
    monkeypatch.setattr(corpus_loader, "SentenceTransformer", lambda _n: _Model())
    monkeypatch.setattr(corpus_loader, "Elasticsearch", _elasticsearch)
    monkeypatch.setattr(corpus_loader, "ensure_es_index", lambda _es: None)
    monkeypatch.setattr(corpus_loader, "write_elasticsearch", lambda *a, **k: None)
    monkeypatch.setattr(
        corpus_loader, "apply_schema", lambda _conn: calls.append("apply_schema")
    )
    monkeypatch.setattr(
        corpus_loader,
        "write_postgres",
        lambda *a, **k: calls.append("write_postgres"),
    )
    monkeypatch.setenv("POSTGRES_PASSWORD", "x")
    monkeypatch.setenv("POLICY_RAG_GATE_ELASTICSEARCH_URL", "http://es.example:9200")

    corpus_loader.main(
        ["--reasoner-id", "fake-reasoner", "--corpus-dir", str(corpus_dir)]
    )

    assert calls.index("apply_schema") < calls.index("register_vector")
    assert calls.index("register_vector") < calls.index("write_postgres")
    assert "elasticsearch:http://es.example:9200" in calls

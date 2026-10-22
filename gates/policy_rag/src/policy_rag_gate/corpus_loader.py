r"""Policy corpus loader — loads one corpus into the reference provider (DR-34).

A corpus is a reference that reasoners' registrations name on their binding
to the RAG policy gate (``PolicyBindingConfig``), held by a retrieval
provider. This loader fills the reference provider's stores with one corpus,
under its ``--corpus-id``: it reads every ``*.md`` file under the corpus
directory, parses YAML frontmatter into ``PolicyDocument``, chunks the body
at ``##`` / ``###`` section boundaries, embeds each chunk using
sentence-transformers (all-MiniLM-L6-v2), and writes:

- PostgreSQL, in the gate's own ``policy_rag`` schema inside the framework's
  database (the gate's DDL is applied before every load):
  ``policy_rag.corpora`` (one row per corpus: the manifest hash of its
  documents, their count, when it was loaded, where from),
  ``policy_rag.documents`` (each document whole, by version, with its front
  matter as metadata, so the provider can return it), and
  ``policy_rag.policy_chunks`` (text + embedding + metadata). Every chunk id
  is deterministic: ``sha256(corpus_id, document_id, version,
  section_path)``.
- Elasticsearch ``policy-chunks`` index (text + metadata, same ids; no
  embedding).

A load replaces the corpus: one Postgres transaction replaces its documents
and chunks, and Elasticsearch gets every chunk by id, after which the
corpus's chunks this load did not write are deleted. ``--wipe`` deletes the
corpus's chunks from Elasticsearch before writing. Two reasoners may bind
the same corpus; it is loaded once.

The policy gate refuses a binding whose corpus is not loaded (the reference
provider cannot describe it), so the order for a new corpus is: load it,
then start the deployment or write the registration that binds it.

Where the documents are: ``--corpus-dir``, or the corpus of one reasoner
(``--reasoner-id``) where the gate reads that reasoner's assets: the
``corpus_dir`` of the composed reasoner plugin whose
``registration.reasoner_id`` matches (plugins filtered by
``DECISIONLEDGER_REASONERS``, as the ledger filters them), else the
reasoner's ``corpus/`` directory under the gate's asset root
(``POLICY_RAG_GATE_ASSETS_ROOT``, DR-33). An explicit ``--corpus-dir`` wins.

Chunking rules (from ``docs/design/gates/policy/policy-corpus.md``):

- Primary split at ``##`` boundaries.
- ``##`` sections exceeding 300 words are split further at ``###`` boundaries.
  The ``section_path`` for a ``###`` chunk is ``"parent > child"`` to prevent
  duplicate paths and ambiguous citations.
- Sections shorter than 30 words are merged with the following sibling.
- Content before the first ``##`` (document preamble) is skipped unless it
  exceeds 30 words, in which case it is indexed under ``"Overview"``.

Usage::

    uv run python -m policy_rag_gate.corpus_loader --corpus-id <corpus-id> \\
        (--reasoner-id <reasoner-id> | --corpus-dir <path>) [--wipe]

Options:
    --corpus-id    The corpus to load (required): the id reasoners' bindings
                   name and every chunk is keyed by.
    --reasoner-id  Read the documents where the gate reads this reasoner's
                   corpus: its composed plugin's ``corpus_dir``, else
                   ``<POLICY_RAG_GATE_ASSETS_ROOT>/<reasoner_id>/corpus``.
    --corpus-dir   Read the documents from this directory (wins over
                   ``--reasoner-id``).
    --wipe         Delete the corpus's chunks from Elasticsearch before
                   loading (Postgres is replaced in one transaction anyway).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import psycopg
import structlog
import yaml
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb
from sentence_transformers import SentenceTransformer

from app.plugins import allowlist_from_settings, discover_plugins
from app.settings import FrameworkSettings
from core.exceptions import ReasonerPluginError
from policy_rag_gate.assets import reasoner_assets
from policy_rag_gate.models.corpus import PolicyDocument
from policy_rag_gate.providers.reference import EMBEDDING_MODEL
from policy_rag_gate.schema import apply_schema
from policy_rag_gate.settings import PolicyRagGateSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.plugin import ReasonerPlugin

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

#: Elasticsearch index definition, shipped as package data next to this module.
_ES_MAPPING_PATH = Path(__file__).resolve().parent / "es" / "policy-chunks.json"

_MODEL_NAME = EMBEDDING_MODEL

#: Name of the shared Elasticsearch BM25 index every corpus is written to.
#: Public so integration fixtures can target the same index the loader and
#: retriever use without reaching into private names.
ES_INDEX = "policy-chunks"
_ES_INDEX = ES_INDEX

_CHUNK_MAX_WORDS = 300
_CHUNK_MIN_WORDS = 30

#: Separator of the fields a chunk id hashes (a byte no field holds).
_ID_SEPARATOR = "\x1f"

# A session-level advisory lock on one corpus, so two loads of it never
# interleave their writes to the two stores (released with the connection).
_CORPUS_LOCK = "select pg_advisory_lock(hashtextextended(%s, 0))"

#: Outcome of ``ensure_es_index``: the index was created fresh, dropped and
#: recreated because its mapping was stale, or left untouched.
EnsureIndexOutcome = Literal["created", "recreated", "kept"]

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_document(path: Path) -> tuple[PolicyDocument, str]:
    """Parse a corpus Markdown file into a PolicyDocument and body text.

    Args:
        path: Path to a corpus Markdown file with YAML frontmatter.

    Returns:
        Tuple of (PolicyDocument, Markdown body with frontmatter stripped).

    Raises:
        ValueError: If the file has missing or malformed frontmatter (YAML
            syntax errors included), or frontmatter that does not validate
            as a ``PolicyDocument``.
    """
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise ValueError(f"{path}: missing YAML frontmatter")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"{path}: malformed YAML frontmatter delimiter")
    try:
        frontmatter = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: malformed YAML frontmatter: {exc}") from exc
    body = parts[2].strip()
    # strict=False: this is a system boundary (YAML → domain model); coercion
    # of StrEnum strings is expected and correct here.
    doc = PolicyDocument.model_validate(frontmatter, strict=False)
    return doc, body


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _word_count(text: str) -> int:
    return len(text.split())


def chunk_document(doc: PolicyDocument, body: str) -> list[dict[str, Any]]:
    """Chunk a document body into indexable records.

    Splits at ``##`` section boundaries, with ``###`` as a secondary split
    for sections exceeding ``_CHUNK_MAX_WORDS``. The ``section_path`` for a
    ``###``-level chunk is ``"H2 heading > H3 heading"`` to prevent duplicate
    paths. Short sections (< ``_CHUNK_MIN_WORDS`` words) are merged with the
    following sibling.

    Args:
        doc: Parsed PolicyDocument providing metadata for each chunk.
        body: Markdown body with frontmatter already stripped.

    Returns:
        List of chunk dicts containing ``policy_id``, ``version``,
        ``jurisdiction``, ``risk_tier``, ``section_path``, ``text``,
        and ``title``. ``chunk_id``, ``span``, and ``embedding`` are
        assigned later (``assign_chunk_ids``, ``assign_spans``).
    """
    # Strip H1 title lines (document-level, not chunk-level content)
    body = re.sub(r"^#(?!#)[^\n]*\n?", "", body, flags=re.MULTILINE).strip()

    # Split at ## boundaries (split just before each ##)
    h2_sections = re.split(r"\n(?=## )", body)

    raw_chunks: list[tuple[str, str]] = []  # (section_path, text)

    for section in h2_sections:
        section = section.strip()
        if not section:
            continue

        # Preamble before the first ## (version lines, synthetic notice, etc.)
        if not section.startswith("## "):
            if _word_count(section) >= _CHUNK_MIN_WORDS:
                raw_chunks.append(("Overview", section))
            continue

        lines = section.split("\n", 1)
        h2_heading = lines[0].lstrip("#").strip()
        h2_body = lines[1].strip() if len(lines) > 1 else ""

        if _word_count(h2_body) <= _CHUNK_MAX_WORDS:
            raw_chunks.append((h2_heading, h2_body))
            continue

        # Section exceeds limit — split at ### boundaries
        h3_parts = re.split(r"\n(?=### )", h2_body)
        preamble = h3_parts[0].strip()

        # Preamble text before the first ### (intro sentences for the section)
        h3_start = 0
        if not preamble.startswith("### "):
            if _word_count(preamble) >= _CHUNK_MIN_WORDS:
                raw_chunks.append((h2_heading, preamble))
            h3_start = 1

        for h3_section in h3_parts[h3_start:]:
            h3_section = h3_section.strip()
            if not h3_section:
                continue
            h3_lines = h3_section.split("\n", 1)
            h3_heading = h3_lines[0].lstrip("#").strip()
            h3_body = h3_lines[1].strip() if len(h3_lines) > 1 else ""
            raw_chunks.append((f"{h2_heading} > {h3_heading}", h3_body))

    # Merge short chunks with the following sibling
    merged: list[tuple[str, str]] = []
    i = 0
    while i < len(raw_chunks):
        path, text = raw_chunks[i]
        if _word_count(text) < _CHUNK_MIN_WORDS and i + 1 < len(raw_chunks):
            _, next_text = raw_chunks[i + 1]
            merged.append((path, f"{text}\n\n{next_text}"))
            i += 2
        else:
            merged.append((path, text))
            i += 1

    return [
        {
            "policy_id": doc.policy_id,
            "title": doc.title,
            "version": doc.version,
            "jurisdiction": doc.jurisdiction,
            "risk_tier": doc.risk_tier,
            "section_path": section_path,
            "text": text,
        }
        for section_path, text in merged
        if text.strip()
    ]


# ---------------------------------------------------------------------------
# Corpus directory resolution
# ---------------------------------------------------------------------------


def resolve_corpus_dir(
    reasoner_id: str,
    corpus_dir: Path | None = None,
    *,
    plugins: Sequence[ReasonerPlugin] | None = None,
    assets_root: Path | None = None,
) -> Path:
    """Return the corpus directory to index for ``reasoner_id``.

    An explicit ``corpus_dir`` wins. Otherwise the reasoner's corpus is
    where the gate reads it: the ``corpus_dir`` of the composed plugin whose
    ``registration.reasoner_id`` equals ``reasoner_id``, else the reasoner's
    ``corpus/`` directory under the asset root (DR-33).

    Args:
        reasoner_id: Reasoner whose corpus is being loaded.
        corpus_dir: Explicit override (``--corpus-dir``). Returned as-is when
            provided.
        plugins: The composed plugins to search. ``None`` discovers the
            installed ones, filtered by ``DECISIONLEDGER_REASONERS`` as the
            ledger filters them; tests inject fakes here.
        assets_root: The gate's asset root (``POLICY_RAG_GATE_ASSETS_ROOT``),
            or ``None`` for none.

    Returns:
        The directory of Markdown policy documents to index.

    Raises:
        ReasonerPluginError: No composed plugin declares ``reasoner_id`` and
            the asset root holds no corpus directory for it (or cannot: the
            id does not name a directory). The message lists the plugin ids
            that were found.
    """
    if corpus_dir is not None:
        return corpus_dir

    found = (
        list(plugins)
        if plugins is not None
        else discover_plugins(allow=allowlist_from_settings(FrameworkSettings()))
    )
    for plugin in found:
        if plugin.registration.reasoner_id == reasoner_id:
            return plugin.corpus_dir

    where = "POLICY_RAG_GATE_ASSETS_ROOT is not set"
    if assets_root is not None:
        try:
            candidate = reasoner_assets(assets_root, reasoner_id).corpus_dir
            present = candidate.is_dir()
        except (ValueError, OSError) as err:
            raise ReasonerPluginError(reasoner_id, str(err)) from err
        if present:
            return candidate
        where = f"the asset root holds no {candidate}"

    discovered = sorted(p.registration.reasoner_id for p in found)
    raise ReasonerPluginError(
        reasoner_id,
        f"no composed reasoner plugin declares reasoner_id {reasoner_id!r} "
        f"(composed: {discovered}) and {where}. Ship the corpus under "
        "<POLICY_RAG_GATE_ASSETS_ROOT>/<reasoner_id>/corpus, or pass --corpus-dir.",
    )


# ---------------------------------------------------------------------------
# Documents, chunk ids, and the manifest
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """``value`` with dates as ISO strings, for the documents' metadata."""
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value


def content_digest(text: str) -> str:
    """SHA-256 (lowercase hex) of ``text`` encoded as UTF-8."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CorpusDocument:
    """One parsed document of the corpus being loaded.

    Attributes:
        document: Its validated front matter.
        body: Its Markdown body (front matter stripped): the text its chunks
            are cut from, stored whole.
        metadata: Its front matter as JSON (dates as ISO strings).
        source_path: Its path relative to the corpus directory.
    """

    document: PolicyDocument
    body: str
    metadata: dict[str, Any]
    source_path: str

    @property
    def content_hash(self) -> str:
        """SHA-256 of ``body``."""
        return content_digest(self.body)


def read_document(path: Path, corpus_dir: Path) -> CorpusDocument:
    """Parse one corpus file for loading.

    Args:
        path: A Markdown file with YAML frontmatter.
        corpus_dir: The corpus directory it was found under.

    Returns:
        The parsed document.

    Raises:
        ValueError: As ``parse_document``, or its front matter holds a value
            JSON cannot carry (the documents table stores it as ``jsonb``).
    """
    document, body = parse_document(path)
    front_matter = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    metadata = _json_safe(dict(front_matter))
    try:
        json.dumps(metadata, allow_nan=False)
    except (TypeError, ValueError) as exc:
        msg = f"{path}: front matter is not JSON-safe ({exc})"
        raise ValueError(msg) from exc
    return CorpusDocument(
        document=document,
        body=body,
        metadata=metadata,
        source_path=path.relative_to(corpus_dir).as_posix(),
    )


def chunk_id(
    corpus_id: str,
    document_id: str,
    version: str,
    section_path: str,
    occurrence: int = 1,
) -> str:
    """The deterministic id of one chunk (DR-34).

    ``sha256(corpus_id, document_id, version, section_path)``: reloading a
    corpus rewrites each chunk under the same id, in both stores. A section
    path a document repeats is told apart by its occurrence (the first one
    keeps the plain id).

    Returns:
        64 lowercase hex characters.
    """
    parts = [corpus_id, document_id, version, section_path]
    if occurrence > 1:
        parts.append(str(occurrence))
    return content_digest(_ID_SEPARATOR.join(parts))


def manifest_hash(documents: Sequence[CorpusDocument]) -> str:
    """The corpus's state marker: SHA-256 over its documents, sorted.

    Each document contributes ``(document_id, version, content_hash,
    metadata_hash)`` — the last the SHA-256 of its front matter as canonical
    JSON — so the hash changes when a document is added, removed,
    re-versioned, or edited, or its front matter changes (a jurisdiction,
    risk tier, or approver retagged without a new version), and not when
    files are renamed or reordered.
    """
    lines = sorted(
        _ID_SEPARATOR.join(
            (
                doc.document.policy_id,
                doc.document.version,
                doc.content_hash,
                content_digest(
                    json.dumps(
                        doc.metadata,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    )
                ),
            )
        )
        for doc in documents
    )
    return content_digest("\n".join(lines))


def duplicate_documents(documents: Sequence[CorpusDocument]) -> list[str]:
    """``document_id@version`` pairs more than one file of the corpus declares."""
    keys = Counter(
        f"{doc.document.policy_id}@{doc.document.version}" for doc in documents
    )
    return sorted(key for key, count in keys.items() if count > 1)


def assign_chunk_ids(corpus_id: str, chunks: list[dict[str, Any]]) -> None:
    """Give one document's chunks their deterministic ids, in place."""
    seen: Counter[str] = Counter()
    for chunk in chunks:
        seen[chunk["section_path"]] += 1
        chunk["chunk_id"] = chunk_id(
            corpus_id,
            chunk["policy_id"],
            chunk["version"],
            chunk["section_path"],
            seen[chunk["section_path"]],
        )


def assign_spans(body: str, chunks: list[dict[str, Any]]) -> None:
    """Locate each chunk's text in the document body, in place.

    A chunk that is a contiguous slice of the body gets its ``(start, end)``
    offsets; one that is not (two short sections merged) gets ``None``.
    Chunks come in body order and do not overlap, so each is searched for
    after the previous one located: text a document repeats (boilerplate
    in two sections) is located where this chunk has it, not where it first
    appears.
    """
    cursor = 0
    for chunk in chunks:
        start = body.find(chunk["text"], cursor)
        if start < 0:
            chunk["span"] = None
            continue
        chunk["span"] = (start, start + len(chunk["text"]))
        cursor = start + len(chunk["text"])


# ---------------------------------------------------------------------------
# Storage — Elasticsearch index lifecycle
# ---------------------------------------------------------------------------


def _corpus_id_is_keyword(mapping_response: Any) -> bool:
    """Return True when the live index maps ``corpus_id`` as ``keyword``.

    Args:
        mapping_response: Result of ``es.indices.get_mapping(index=...)``;
            either the client's response object (``.body`` holds the dict)
            or a plain dict keyed by index name.
    """
    body = getattr(mapping_response, "body", mapping_response)
    for index_def in body.values():
        properties = index_def.get("mappings", {}).get("properties", {})
        field = properties.get("corpus_id")
        if field is None or field.get("type") != "keyword":
            return False
    return bool(body)


def ensure_es_index(
    es: Elasticsearch, *, mapping_path: Path = _ES_MAPPING_PATH
) -> EnsureIndexOutcome:
    """Create the Elasticsearch index, recreating it if its mapping is stale.

    The corpus filter is a ``term`` query on ``corpus_id``; that only
    matches when the field is mapped as ``keyword``. An index created before
    DR-34 (keyed by ``reasoner_id``), or before the mapping declared the
    field, has no keyword ``corpus_id``, which silently returns zero sparse
    hits. Such an index is dropped and recreated — wiping every corpus's
    BM25 documents — so the caller logs loudly that each corpus must be
    reloaded.

    Args:
        es: Connected Elasticsearch client.
        mapping_path: JSON file holding the index ``settings`` and
            ``mappings``.

    Returns:
        ``"created"`` when no index existed, ``"recreated"`` when a stale
        index was replaced, ``"kept"`` when the existing mapping is current.
    """
    outcome: EnsureIndexOutcome = "created"
    if es.indices.exists(index=_ES_INDEX):
        current = es.indices.get_mapping(index=_ES_INDEX)
        if _corpus_id_is_keyword(current):
            return "kept"
        log.warning(
            "es.index_mapping_stale",
            component="corpus_loader",
            index=_ES_INDEX,
            reason="corpus_id is not mapped as keyword; recreating the index",
            action=(
                "every corpus must be reloaded: run the corpus loader once per "
                "--corpus-id"
            ),
        )
        es.indices.delete(index=_ES_INDEX)
        outcome = "recreated"

    mapping = json.loads(mapping_path.read_text())
    es.indices.create(
        index=_ES_INDEX,
        settings=mapping["settings"],
        mappings=mapping["mappings"],
    )
    log.info("es.index_created", component="corpus_loader", index=_ES_INDEX)
    return outcome


#: Backward-compatible alias; ``ensure_es_index`` is the public name.
_ensure_es_index = ensure_es_index


# ---------------------------------------------------------------------------
# Storage — PostgreSQL
# ---------------------------------------------------------------------------

_UPSERT_CORPUS = """
    insert into policy_rag.corpora
        (corpus_id, manifest_hash, document_count, loaded_at, source_dir)
    values (%s, %s, %s, now(), %s)
    on conflict (corpus_id) do update set
        manifest_hash = excluded.manifest_hash,
        document_count = excluded.document_count,
        loaded_at = excluded.loaded_at,
        source_dir = excluded.source_dir
"""

_INSERT_DOCUMENT = """
    insert into policy_rag.documents
        (corpus_id, document_id, document_version, title, content_hash, body,
         metadata, source_path)
    values (%s, %s, %s, %s, %s, %s, %s, %s)
"""

_INSERT_CHUNK = """
    insert into policy_rag.policy_chunks
        (chunk_id, corpus_id, policy_id, policy_version, title, jurisdiction,
         risk_tier, section_path, chunk_text, span_start, span_end, embedding)
    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def write_postgres(
    conn: psycopg.Connection,
    *,
    corpus_id: str,
    source_dir: Path,
    documents: Sequence[CorpusDocument],
    chunks: Sequence[dict[str, Any]],
) -> None:
    """Replace one corpus in the reference provider's Postgres tables.

    Upserts the corpus's ``corpora`` row (its manifest hash, document count,
    load time, and source directory), deletes its documents (and, by
    cascade, its chunks), and inserts the documents and chunks given. The
    caller commits, so readers see the old corpus or the new one.

    Args:
        conn: Open psycopg connection with the pgvector adapter registered.
        corpus_id: The corpus being loaded.
        source_dir: Where its documents were read from.
        documents: Its parsed documents.
        chunks: Their chunks, each with ``chunk_id``, ``span``, and
            ``embedding``.
    """
    with conn.cursor() as cur:
        cur.execute(
            _UPSERT_CORPUS,
            (corpus_id, manifest_hash(documents), len(documents), str(source_dir)),
        )
        cur.execute(
            "DELETE FROM policy_rag.documents WHERE corpus_id = %s", (corpus_id,)
        )
        cur.executemany(
            _INSERT_DOCUMENT,
            [
                (
                    corpus_id,
                    doc.document.policy_id,
                    doc.document.version,
                    doc.document.title,
                    doc.content_hash,
                    doc.body,
                    Jsonb(doc.metadata),
                    doc.source_path,
                )
                for doc in documents
            ],
        )
        cur.executemany(
            _INSERT_CHUNK,
            [
                (
                    c["chunk_id"],
                    corpus_id,
                    c["policy_id"],
                    c["version"],
                    c["title"],
                    c["jurisdiction"],
                    c["risk_tier"],
                    c["section_path"],
                    c["text"],
                    c["span"][0] if c["span"] is not None else None,
                    c["span"][1] if c["span"] is not None else None,
                    c["embedding"],
                )
                for c in chunks
            ],
        )


# ---------------------------------------------------------------------------
# Storage — Elasticsearch documents
# ---------------------------------------------------------------------------


def write_elasticsearch(
    es: Elasticsearch,
    chunks: Sequence[dict[str, Any]],
    *,
    corpus_id: str,
    wipe: bool = False,
) -> int:
    """Replace one corpus's chunks in the ``policy-chunks`` index.

    Embeddings are not written to ES — only text and metadata fields. Every
    chunk is indexed under its deterministic id (overwriting the copy an
    earlier load wrote), then the corpus's chunks this load did not write
    (a removed section or document) are deleted.

    Args:
        es: Connected Elasticsearch client.
        chunks: Chunk dicts (``embedding`` is ignored).
        corpus_id: The corpus, stored on each document so the retriever can
            filter by it.
        wipe: Delete every chunk of the corpus before writing.

    Returns:
        How many stale chunks were deleted after writing.
    """
    corpus_filter = {"term": {"corpus_id": corpus_id}}
    if wipe:
        es.delete_by_query(index=_ES_INDEX, query=corpus_filter, refresh=True)
        log.info("corpus_loader.es_wiped", corpus_id=corpus_id)
    actions = [
        {
            "_index": _ES_INDEX,
            "_id": c["chunk_id"],
            "_source": {
                "chunk_id": c["chunk_id"],
                "corpus_id": corpus_id,
                "policy_id": c["policy_id"],
                "title": c["title"],
                "version": c["version"],
                "jurisdiction": c["jurisdiction"],
                "risk_tier": c["risk_tier"],
                "section_path": c["section_path"],
                "text": c["text"],
                "span_start": c["span"][0] if c["span"] is not None else None,
                "span_end": c["span"][1] if c["span"] is not None else None,
            },
        }
        for c in chunks
    ]
    if actions:
        bulk(es, actions)
    response = es.delete_by_query(
        index=_ES_INDEX,
        query={
            "bool": {
                "filter": [corpus_filter],
                "must_not": [{"ids": {"values": [c["chunk_id"] for c in chunks]}}],
            }
        },
        refresh=True,
    )
    return int(response.get("deleted", 0)) if hasattr(response, "get") else 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for ``python -m policy_rag_gate.corpus_loader``."""
    parser = argparse.ArgumentParser(
        description=(
            "Load one policy corpus into the RAG policy gate's reference "
            "retrieval provider (pgvector and Elasticsearch)."
        )
    )
    parser.add_argument(
        "--corpus-id",
        required=True,
        help=(
            "The corpus to load: the id reasoners' bindings to the policy gate "
            "name (corpus_id). Letters, digits, '.', '_', and '-'."
        ),
    )
    parser.add_argument(
        "--reasoner-id",
        default=None,
        help=(
            "Read the documents where the gate reads this reasoner's corpus: "
            "the corpus_dir of its composed plugin, else "
            "<POLICY_RAG_GATE_ASSETS_ROOT>/<reasoner-id>/corpus."
        ),
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=None,
        help="Read the documents from this directory (wins over --reasoner-id).",
    )
    parser.add_argument(
        "--wipe",
        action="store_true",
        help=(
            "Delete the corpus's chunks from Elasticsearch before loading. "
            "Default: write every chunk by id, then delete the corpus's chunks "
            "the load did not write (Postgres is replaced in one transaction "
            "either way)."
        ),
    )
    return parser


_CORPUS_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def main(argv: list[str] | None = None) -> None:
    """Load one policy corpus into pgvector and Elasticsearch.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if not _CORPUS_ID.fullmatch(args.corpus_id) or len(args.corpus_id) > 128:
        parser.error(
            f"--corpus-id {args.corpus_id!r} is not a corpus id (letters, digits, "
            "'.', '_', and '-', starting with a letter or digit; at most 128)"
        )
    if args.corpus_dir is None and args.reasoner_id is None:
        parser.error("say where the documents are: --reasoner-id or --corpus-dir")

    # Read the DSN from FrameworkSettings (the gate's schema lives in the
    # framework's database) and the ES URL and asset root from the gate's own
    # settings, so the loader honors whatever the local .env / docker-compose
    # stack configured. Avoids silently mismatching when POSTGRES_PASSWORD is
    # anything but the docker-compose default.
    framework_settings = FrameworkSettings()
    gate_settings = PolicyRagGateSettings()
    pg_dsn = framework_settings.postgres_dsn
    es_url = gate_settings.elasticsearch_url

    if args.corpus_dir is not None:
        corpus_dir: Path = args.corpus_dir
    else:
        try:
            corpus_dir = resolve_corpus_dir(
                args.reasoner_id,
                plugins=discover_plugins(
                    allow=allowlist_from_settings(framework_settings)
                ),
                assets_root=gate_settings.assets_root_path,
            )
        except ReasonerPluginError as exc:
            log.error("corpus_loader.unknown_reasoner", error=str(exc))
            sys.exit(2)

    log.info(
        "corpus_loader.start",
        corpus_id=args.corpus_id,
        reasoner_id=args.reasoner_id,
        corpus_dir=str(corpus_dir),
        wipe=args.wipe,
    )

    # Discover corpus files
    md_files = sorted(corpus_dir.rglob("*.md"))
    if not md_files:
        log.error("corpus_loader.no_files", corpus_dir=str(corpus_dir))
        sys.exit(1)
    log.info("corpus_loader.files_found", count=len(md_files))

    # Parse and chunk all documents
    documents: list[CorpusDocument] = []
    all_chunks: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for path in md_files:
        try:
            parsed = read_document(path, corpus_dir)
        except (ValueError, KeyError) as exc:
            log.error("corpus_loader.parse_error", path=str(path), error=str(exc))
            unreadable.append(path.relative_to(corpus_dir).as_posix())
            continue
        documents.append(parsed)
        chunks = chunk_document(parsed.document, parsed.body)
        assign_chunk_ids(args.corpus_id, chunks)
        assign_spans(parsed.body, chunks)
        all_chunks.extend(chunks)
        log.debug(
            "corpus_loader.document_chunked",
            policy_id=parsed.document.policy_id,
            chunks=len(chunks),
        )

    if unreadable:
        # A load replaces the corpus: loading without a file that does not
        # parse would silently drop its document from both stores.
        log.error(
            "corpus_loader.unreadable_documents",
            corpus_dir=str(corpus_dir),
            files=unreadable,
            reason="a load replaces the corpus; fix or remove these files",
        )
        sys.exit(1)
    if not documents:
        log.error("corpus_loader.no_documents", corpus_dir=str(corpus_dir))
        sys.exit(1)
    duplicates = duplicate_documents(documents)
    if duplicates:
        log.error(
            "corpus_loader.duplicate_documents",
            corpus_dir=str(corpus_dir),
            documents=duplicates,
            reason="two files declare one policy_id and version",
        )
        sys.exit(1)
    log.info("corpus_loader.chunks_total", count=len(all_chunks))

    # Load the embedding model and embed all chunks in one batch (faster than
    # per-chunk calls)
    log.info("corpus_loader.model_loading", model=_MODEL_NAME)
    model = SentenceTransformer(_MODEL_NAME)
    log.info("corpus_loader.embedding_start", batch_size=64)
    texts = [c["text"] for c in all_chunks]
    embeddings = model.encode(texts, show_progress_bar=True, batch_size=64)
    for chunk, embedding in zip(all_chunks, embeddings, strict=True):
        chunk["embedding"] = embedding.tolist()
    log.info("corpus_loader.embedding_done")

    # One load of a corpus at a time, across both stores: a session-level
    # advisory lock on its id, held until Elasticsearch is written too.
    with psycopg.connect(pg_dsn, autocommit=True) as lock_conn:
        lock_conn.execute(_CORPUS_LOCK, (f"policy_rag.corpus:{args.corpus_id}",))

        # Write to PostgreSQL. The gate's DDL is applied first (idempotent,
        # and refusing a schema from before DR-34), so a fresh database gets
        # the vector extension and the policy_rag schema before the pgvector
        # adapters are registered and the first insert runs.
        with psycopg.connect(pg_dsn) as conn:
            apply_schema(conn)
            register_vector(conn)
            write_postgres(
                conn,
                corpus_id=args.corpus_id,
                source_dir=corpus_dir.resolve(),
                documents=documents,
                chunks=all_chunks,
            )
            conn.commit()

        log.info(
            "corpus_loader.postgres_done",
            corpus_id=args.corpus_id,
            documents=len(documents),
            chunks=len(all_chunks),
            manifest_hash=manifest_hash(documents),
        )

        # Write to Elasticsearch
        es = Elasticsearch(es_url)
        ensure_es_index(es)
        stale = write_elasticsearch(
            es, all_chunks, corpus_id=args.corpus_id, wipe=args.wipe
        )
        log.info("corpus_loader.es_done", chunks=len(all_chunks), stale_deleted=stale)

    log.info(
        "corpus_loader.complete",
        corpus_id=args.corpus_id,
        documents=len(documents),
        chunks=len(all_chunks),
    )


if __name__ == "__main__":
    main()

r"""Policy corpus loader.

Reads every ``*.md`` file under a reasoner's corpus directory, parses YAML
frontmatter into ``PolicyDocument``, chunks the body at ``##`` / ``###``
section boundaries, embeds each chunk using sentence-transformers
(all-MiniLM-L6-v2), and writes to:

- PostgreSQL ``policy_rag.policy_chunks`` (text + embedding + metadata,
  including ``reasoner_id`` for tenant filtering at retrieval time and the
  document ``title`` so the retriever can cite without reading the corpus).
  The table lives in the gate's own schema inside the framework's database;
  the gate's DDL is applied before every load.
- Elasticsearch ``policy-chunks`` index (text + metadata; no embedding).

The policy gate owns both stores; each reasoner owns its corpus directory.
The loader is invoked with ``--reasoner-id`` and resolves the directory from
the installed ``ReasonerPlugin`` whose ``registration.reasoner_id`` matches,
so the gate never hardcodes a corpus path. ``--corpus-dir`` overrides that
lookup (useful for tests and for indexing a corpus before its plugin is
installed).

Chunking rules (from ``docs/design/policy-corpus.md``):

- Primary split at ``##`` boundaries.
- ``##`` sections exceeding 300 words are split further at ``###`` boundaries.
  The ``section_path`` for a ``###`` chunk is ``"parent > child"`` to prevent
  duplicate paths and ambiguous citations.
- Sections shorter than 30 words are merged with the following sibling.
- Content before the first ``##`` (document preamble) is skipped unless it
  exceeds 30 words, in which case it is indexed under ``"Overview"``.

Usage::

    uv run python -m policy_rag_gate.corpus_loader \\
        --reasoner-id <reasoner-id> [--corpus-dir <path>] [--wipe]

Options:
    --reasoner-id  Reasoner that owns this corpus. Stored on every chunk so
                   the retriever can filter by tenant, and used to resolve
                   the corpus directory from the matching installed plugin.
    --corpus-dir   Explicit corpus root. When omitted, the matching plugin's
                   ``corpus_dir`` is used.
    --wipe         Delete this reasoner's chunks from both stores before
                   loading. Default behavior is to delete existing chunks for
                   each ``(reasoner_id, policy_id)`` before inserting, making
                   repeated runs safe without accumulating duplicates.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import psycopg
import structlog
import yaml
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from app.plugins import discover_plugins
from app.settings import FrameworkSettings
from core.exceptions import ReasonerPluginError
from policy_rag_gate.models.corpus import PolicyDocument
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

_MODEL_NAME = "all-MiniLM-L6-v2"

#: Name of the shared Elasticsearch BM25 index every reasoner's corpus is
#: written to. Public so integration fixtures can target the same index the
#: loader and retriever use without reaching into private names.
ES_INDEX = "policy-chunks"
_ES_INDEX = ES_INDEX

_CHUNK_MAX_WORDS = 300
_CHUNK_MIN_WORDS = 30

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
        and ``title``. The ``embedding`` field is populated later.
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
            "chunk_id": str(uuid.uuid4()),
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
) -> Path:
    """Return the corpus directory to index for ``reasoner_id``.

    An explicit ``corpus_dir`` wins. Otherwise the installed reasoner plugins
    are discovered and the one whose ``registration.reasoner_id`` equals
    ``reasoner_id`` supplies its ``corpus_dir``.

    Args:
        reasoner_id: Reasoner whose corpus is being loaded.
        corpus_dir: Explicit override (``--corpus-dir``). Returned as-is when
            provided.
        plugins: Plugins to search instead of calling ``discover_plugins()``.
            Tests inject fakes here; production leaves it ``None``.

    Returns:
        The directory of Markdown policy documents to index.

    Raises:
        ReasonerPluginError: No installed plugin declares ``reasoner_id``.
            The message lists the ids that were discovered.
    """
    if corpus_dir is not None:
        return corpus_dir

    found = list(plugins) if plugins is not None else discover_plugins()
    for plugin in found:
        if plugin.registration.reasoner_id == reasoner_id:
            return plugin.corpus_dir

    discovered = sorted(p.registration.reasoner_id for p in found)
    raise ReasonerPluginError(
        reasoner_id,
        f"no installed reasoner plugin declares reasoner_id {reasoner_id!r}; "
        f"discovered: {discovered}. Install the plugin or pass --corpus-dir.",
    )


# ---------------------------------------------------------------------------
# Storage — Elasticsearch index lifecycle
# ---------------------------------------------------------------------------


def _reasoner_id_is_keyword(mapping_response: Any) -> bool:
    """Return True when the live index maps ``reasoner_id`` as ``keyword``.

    Args:
        mapping_response: Result of ``es.indices.get_mapping(index=...)``;
            either the client's response object (``.body`` holds the dict)
            or a plain dict keyed by index name.
    """
    body = getattr(mapping_response, "body", mapping_response)
    for index_def in body.values():
        properties = index_def.get("mappings", {}).get("properties", {})
        field = properties.get("reasoner_id")
        if field is None or field.get("type") != "keyword":
            return False
    return bool(body)


def ensure_es_index(
    es: Elasticsearch, *, mapping_path: Path = _ES_MAPPING_PATH
) -> EnsureIndexOutcome:
    """Create the Elasticsearch index, recreating it if its mapping is stale.

    The tenant filter is a ``term`` query on ``reasoner_id``; that only
    matches when the field is mapped as ``keyword``. An index created before
    the mapping declared ``reasoner_id`` has it dynamically mapped as
    ``text``, which silently returns zero sparse hits for hyphenated ids.
    Such an index is dropped and recreated — wiping every reasoner's BM25
    documents — so the caller logs loudly that each corpus must be reloaded.

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
        if _reasoner_id_is_keyword(current):
            return "kept"
        log.warning(
            "es.index_mapping_stale",
            component="corpus_loader",
            index=_ES_INDEX,
            reason="reasoner_id is not mapped as keyword; recreating the index",
            action=(
                "every reasoner's corpus must be reloaded: run the corpus "
                "loader once per --reasoner-id"
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


def write_postgres(
    conn: psycopg.Connection,
    chunks: list[dict[str, Any]],
    *,
    reasoner_id: str,
    policy_id: str,
    wipe: bool,
) -> None:
    """Write chunks for one policy document to ``policy_chunks``.

    When ``wipe`` is False, existing chunks for ``(reasoner_id, policy_id)``
    are deleted before inserting so repeated runs do not accumulate duplicates.

    Args:
        conn: Open psycopg connection with pgvector adapter registered.
        chunks: Chunk dicts including ``embedding`` as a list of floats.
        reasoner_id: Reasoner that owns this corpus.
        policy_id: Document identifier — used for the pre-insert delete.
        wipe: When True, skip the per-document delete (caller truncated).
    """
    with conn.cursor() as cur:
        if not wipe:
            cur.execute(
                "DELETE FROM policy_rag.policy_chunks "
                "WHERE reasoner_id = %s AND policy_id = %s",
                (reasoner_id, policy_id),
            )
        cur.executemany(
            """
            INSERT INTO policy_rag.policy_chunks
                (chunk_id, reasoner_id, policy_id, policy_version, title,
                 jurisdiction, risk_tier, section_path, chunk_text, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    c["chunk_id"],
                    reasoner_id,
                    c["policy_id"],
                    c["version"],
                    c["title"],
                    c["jurisdiction"],
                    c["risk_tier"],
                    c["section_path"],
                    c["text"],
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
    chunks: list[dict[str, Any]],
    *,
    reasoner_id: str,
) -> None:
    """Bulk-index chunks into the ``policy-chunks`` Elasticsearch index.

    Embeddings are not written to ES — only text and metadata fields.

    Args:
        es: Connected Elasticsearch client.
        chunks: Chunk dicts (``embedding`` field is ignored).
        reasoner_id: Reasoner that owns this corpus, stored on each document
            so the gate's retriever can filter by tenant.
    """
    actions = [
        {
            "_index": _ES_INDEX,
            "_id": c["chunk_id"],
            "_source": {
                "chunk_id": c["chunk_id"],
                "reasoner_id": reasoner_id,
                "policy_id": c["policy_id"],
                "title": c["title"],
                "version": c["version"],
                "jurisdiction": c["jurisdiction"],
                "risk_tier": c["risk_tier"],
                "section_path": c["section_path"],
                "text": c["text"],
            },
        }
        for c in chunks
    ]
    if actions:
        bulk(es, actions)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for ``python -m policy_rag_gate.corpus_loader``."""
    parser = argparse.ArgumentParser(
        description="Load a reasoner's policy corpus into pgvector and Elasticsearch."
    )
    parser.add_argument(
        "--reasoner-id",
        required=True,
        help=(
            "Reasoner that owns this corpus. Stored on every chunk so the "
            "retriever can filter by tenant, and used to find the installed "
            "plugin whose corpus_dir is indexed."
        ),
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=None,
        help=(
            "Explicit corpus root. Default: the corpus_dir of the installed "
            "plugin whose registration.reasoner_id matches --reasoner-id."
        ),
    )
    parser.add_argument(
        "--wipe",
        action="store_true",
        help=(
            "Delete this reasoner's chunks from both stores before loading. "
            "Default: delete existing chunks per (reasoner_id, policy_id) "
            "before inserting (safe for repeated runs)."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    """Load one reasoner's policy corpus into pgvector and Elasticsearch.

    Args:
        argv: Optional argv override (defaults to ``sys.argv[1:]``).
    """
    args = build_parser().parse_args(argv)

    try:
        corpus_dir = resolve_corpus_dir(args.reasoner_id, args.corpus_dir)
    except ReasonerPluginError as exc:
        log.error("corpus_loader.unknown_reasoner", error=str(exc))
        sys.exit(2)

    # Read the DSN from FrameworkSettings (the gate's schema lives in the
    # framework's database) and the ES URL from the gate's own settings, so
    # the loader honors whatever the local .env / docker-compose stack
    # configured. Avoids silently mismatching when POSTGRES_PASSWORD is
    # anything but the docker-compose default.
    pg_dsn = FrameworkSettings().postgres_dsn
    es_url = PolicyRagGateSettings().elasticsearch_url

    log.info(
        "corpus_loader.start",
        reasoner_id=args.reasoner_id,
        corpus_dir=str(corpus_dir),
        wipe=args.wipe,
    )

    # Load embedding model
    log.info("corpus_loader.model_loading", model=_MODEL_NAME)
    model = SentenceTransformer(_MODEL_NAME)

    # Discover corpus files
    md_files = sorted(corpus_dir.rglob("*.md"))
    if not md_files:
        log.error("corpus_loader.no_files", corpus_dir=str(corpus_dir))
        sys.exit(1)
    log.info("corpus_loader.files_found", count=len(md_files))

    # Parse and chunk all documents
    all_chunks: list[dict[str, Any]] = []
    skipped = 0
    for path in md_files:
        try:
            doc, body = parse_document(path)
        except (ValueError, KeyError) as exc:
            log.warning("corpus_loader.parse_error", path=str(path), error=str(exc))
            skipped += 1
            continue
        chunks = chunk_document(doc, body)
        all_chunks.extend(chunks)
        log.debug(
            "corpus_loader.document_chunked",
            policy_id=doc.policy_id,
            chunks=len(chunks),
        )

    if skipped:
        log.warning("corpus_loader.files_skipped", count=skipped)
    log.info("corpus_loader.chunks_total", count=len(all_chunks))

    # Embed all chunks in one batch (faster than per-chunk calls)
    log.info("corpus_loader.embedding_start", batch_size=64)
    texts = [c["text"] for c in all_chunks]
    embeddings = model.encode(texts, show_progress_bar=True, batch_size=64)
    for chunk, embedding in zip(all_chunks, embeddings, strict=True):
        chunk["embedding"] = embedding.tolist()
    log.info("corpus_loader.embedding_done")

    # Write to PostgreSQL. The gate's DDL is applied first (idempotent), so a
    # fresh database gets the vector extension and the policy_rag schema
    # before the pgvector adapters are registered and the first insert runs.
    with psycopg.connect(pg_dsn) as conn:
        apply_schema(conn)
        register_vector(conn)

        if args.wipe:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM policy_rag.policy_chunks WHERE reasoner_id = %s",
                    (args.reasoner_id,),
                )
            log.info("corpus_loader.postgres_wiped", reasoner_id=args.reasoner_id)

        by_policy: dict[str, list[dict[str, Any]]] = {}
        for chunk in all_chunks:
            by_policy.setdefault(chunk["policy_id"], []).append(chunk)

        for policy_id, chunks in by_policy.items():
            write_postgres(
                conn,
                chunks,
                reasoner_id=args.reasoner_id,
                policy_id=policy_id,
                wipe=args.wipe,
            )
            log.debug(
                "corpus_loader.postgres_written",
                policy_id=policy_id,
                count=len(chunks),
            )

        conn.commit()

    log.info("corpus_loader.postgres_done", chunks=len(all_chunks))

    # Write to Elasticsearch
    es = Elasticsearch(es_url)
    ensure_es_index(es)

    if args.wipe:
        es.delete_by_query(
            index=_ES_INDEX,
            query={"term": {"reasoner_id": args.reasoner_id}},
            refresh=True,
        )
        log.info("corpus_loader.es_wiped", reasoner_id=args.reasoner_id)

    write_elasticsearch(es, all_chunks, reasoner_id=args.reasoner_id)
    log.info("corpus_loader.es_done", chunks=len(all_chunks))

    log.info(
        "corpus_loader.complete",
        documents=len(md_files) - skipped,
        chunks=len(all_chunks),
    )


if __name__ == "__main__":
    main()

r"""Policy corpus loader.

Reads all ``corpus/**/*.md`` files, parses YAML frontmatter into
``PolicyDocument``, chunks the body at ``##`` / ``###`` section boundaries,
embeds each chunk using sentence-transformers (all-MiniLM-L6-v2), and writes to:

- PostgreSQL ``decisionledger.policy_chunks`` (text + embedding + metadata,
  including ``reasoner_id`` for tenant filtering at retrieval time).
- Elasticsearch ``policy-chunks`` index (text + metadata; no embedding).

The framework owns the schema; the loader is invoked with ``--reasoner-id``
to tag every chunk with its owning reasoner's identity so a multi-tenant
deployment can filter at retrieval time.

Chunking rules (from ``docs/design/policy-corpus.md``):

- Primary split at ``##`` boundaries.
- ``##`` sections exceeding 300 words are split further at ``###`` boundaries.
  The ``section_path`` for a ``###`` chunk is ``"parent > child"`` to prevent
  duplicate paths and ambiguous citations.
- Sections shorter than 30 words are merged with the following sibling.
- Content before the first ``##`` (document preamble) is skipped unless it
  exceeds 30 words, in which case it is indexed under ``"Overview"``.

Usage::

    uv run python -m app.retrieval.corpus_loader \\
        --reasoner-id ato-reasoner [--wipe]

Options:
    --reasoner-id  Reasoner that owns this corpus (e.g., ``ato-reasoner``).
                   Stored on every chunk so the retriever can filter by tenant.
    --wipe         Truncate both stores before loading. Default behaviour is
                   to delete existing chunks for each ``(reasoner_id,
                   policy_id)`` before inserting, making repeated runs safe
                   without accumulating duplicates.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import psycopg
import structlog
import yaml
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from app.settings import FrameworkSettings
from core.gate import PolicyDocument

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parents[2]
_CORPUS_DIR = _PROJECT_ROOT / "corpus"
_ES_MAPPING_PATH = _PROJECT_ROOT / "infra" / "elasticsearch" / "policy-chunks.json"

_MODEL_NAME = "all-MiniLM-L6-v2"
_ES_INDEX = "policy-chunks"

_CHUNK_MAX_WORDS = 300
_CHUNK_MIN_WORDS = 30

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
        ValueError: If the file is missing or has malformed frontmatter.
    """
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise ValueError(f"{path}: missing YAML frontmatter")
    parts = raw.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"{path}: malformed YAML frontmatter delimiter")
    frontmatter = yaml.safe_load(parts[1])
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
# Storage — PostgreSQL
# ---------------------------------------------------------------------------


def _ensure_es_index(es: Elasticsearch) -> None:
    """Create the Elasticsearch index if it does not already exist.

    Args:
        es: Connected Elasticsearch client.
    """
    if es.indices.exists(index=_ES_INDEX):
        return
    mapping = json.loads(_ES_MAPPING_PATH.read_text())
    es.indices.create(
        index=_ES_INDEX,
        settings=mapping["settings"],
        mappings=mapping["mappings"],
    )
    log.info("es.index_created", index=_ES_INDEX)


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
        reasoner_id: Reasoner that owns this corpus (e.g., ``"ato-reasoner"``).
        policy_id: Document identifier — used for the pre-insert delete.
        wipe: When True, skip the per-document delete (caller truncated).
    """
    with conn.cursor() as cur:
        if not wipe:
            cur.execute(
                "DELETE FROM decisionledger.policy_chunks "
                "WHERE reasoner_id = %s AND policy_id = %s",
                (reasoner_id, policy_id),
            )
        cur.executemany(
            """
            INSERT INTO decisionledger.policy_chunks
                (chunk_id, reasoner_id, policy_id, policy_version,
                 jurisdiction, risk_tier, section_path, chunk_text, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    c["chunk_id"],
                    reasoner_id,
                    c["policy_id"],
                    c["version"],
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
# Storage — Elasticsearch
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
            so the framework retriever can filter by tenant.
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


def main() -> None:
    """Load the full policy corpus into pgvector and Elasticsearch."""
    parser = argparse.ArgumentParser(
        description="Load policy corpus into pgvector and Elasticsearch."
    )
    parser.add_argument(
        "--reasoner-id",
        required=True,
        help=(
            "Reasoner that owns this corpus (e.g., 'ato-reasoner'). Stored "
            "on every chunk so the retriever can filter by tenant."
        ),
    )
    parser.add_argument(
        "--wipe",
        action="store_true",
        help=(
            "Truncate both stores before loading. Default: delete existing "
            "chunks per (reasoner_id, policy_id) before inserting (safe for "
            "repeated runs)."
        ),
    )
    args = parser.parse_args()

    # Build DSN + ES URL from FrameworkSettings so the loader honors whatever
    # the local .env / docker-compose stack actually configured. Avoids
    # silently mismatching when POSTGRES_PASSWORD is anything but the
    # docker-compose default.
    fw = FrameworkSettings()
    pg_dsn = fw.postgres_dsn
    es_url = fw.elasticsearch_url

    log.info("corpus_loader.start", reasoner_id=args.reasoner_id, wipe=args.wipe)

    # Load embedding model
    log.info("corpus_loader.model_loading", model=_MODEL_NAME)
    model = SentenceTransformer(_MODEL_NAME)

    # Discover corpus files
    md_files = sorted(_CORPUS_DIR.rglob("*.md"))
    if not md_files:
        log.error("corpus_loader.no_files", corpus_dir=str(_CORPUS_DIR))
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

    # Write to PostgreSQL
    with psycopg.connect(pg_dsn) as conn:
        register_vector(conn)

        if args.wipe:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM decisionledger.policy_chunks WHERE reasoner_id = %s",
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
    _ensure_es_index(es)

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

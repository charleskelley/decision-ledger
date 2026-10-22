-- =============================================================================
-- RAG policy gate — table definitions (the reference retrieval provider)
-- Depends on 01_init.sql (vector extension + policy_rag schema).
--
-- DR-34: a corpus is a reference a reasoner's binding names, held by a
-- retrieval provider. These tables are the reference provider's: the corpus
-- loader writes one corpus at a time, under its corpus_id, and the provider
-- retrieves from it. Which corpora a reasoner reads is its registration's
-- binding, not a column here. A schema from before DR-34 (chunks keyed by
-- reasoner_id) is refused by policy_rag_gate/schema.py: drop the schema and
-- reload each corpus.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- corpora — one row per corpus the reference provider holds
--
-- manifest_hash identifies the corpus's current content: the SHA-256 over its
-- documents' (document_id, version, content_hash), sorted. The provider
-- reports it as the corpus's state marker in every query the gate records.
-- source_dir is the directory the loader last read it from.
-- ---------------------------------------------------------------------------
create table if not exists policy_rag.corpora (
    corpus_id text primary key,
    manifest_hash text not null,
    document_count integer not null check (document_count >= 0),
    loaded_at timestamptz not null default now(),
    source_dir text not null
);

-- ---------------------------------------------------------------------------
-- documents — every document of every corpus, whole, by version
--
-- body is the document text (front matter stripped) the chunks were cut
-- from; content_hash is its SHA-256. metadata is the document's front matter
-- as JSON (any lineage its owner put there: an approver, an effective date),
-- returned with every passage. source_path is relative to the corpus's
-- source_dir. Kept so the provider can return a document whole.
-- ---------------------------------------------------------------------------
create table if not exists policy_rag.documents (
    corpus_id text not null
    references policy_rag.corpora (corpus_id) on delete cascade,
    document_id text not null,
    document_version text not null,
    title text not null,
    content_hash text not null,
    body text not null,
    metadata jsonb not null default '{}'::jsonb,
    source_path text not null,
    primary key (corpus_id, document_id, document_version)
);

-- ---------------------------------------------------------------------------
-- policy_chunks — pgvector HNSW index for dense policy retrieval
--
-- Each row is one paragraph-level chunk of a document. chunk_id is
-- deterministic: sha256(corpus_id, document_id, version, section_path), so a
-- reload replaces a chunk by id in both stores instead of duplicating it. The
-- retriever filters by corpus_id, jurisdiction, and risk_tier before the HNSW
-- index is queried. span_start/span_end locate chunk_text in the document's
-- body when the chunk is a contiguous slice of it (null otherwise). The
-- embedding column dimensionality must match the sentence-transformer model
-- in use. Current model: all-MiniLM-L6-v2 → 384 dimensions. Update
-- vector(384) here and the model in the reference provider and the corpus
-- loader together if the model changes.
--
-- risk_tier is nullable: null means the chunk applies to all tiers.
-- title is the owning document's human-readable title, denormalized onto
-- every chunk so the retriever can build a citation without a join.
-- ---------------------------------------------------------------------------
create table if not exists policy_rag.policy_chunks (
    chunk_id text primary key,
    corpus_id text not null,
    policy_id text not null,
    policy_version text not null,
    title text not null default '',
    jurisdiction text not null,
    risk_tier text,
    section_path text not null,
    chunk_text text not null,
    span_start integer,
    span_end integer,
    embedding vector(384) not null,
    created_at timestamptz not null default now(),
    foreign key (corpus_id, policy_id, policy_version)
    references policy_rag.documents (corpus_id, document_id, document_version)
    on delete cascade,
    check ((span_start is null) = (span_end is null)),
    check (span_start is null or (0 <= span_start and span_start <= span_end))
);

create index if not exists idx_policy_chunks_hnsw
on policy_rag.policy_chunks
using hnsw (embedding vector_cosine_ops);

create index if not exists idx_policy_chunks_corpus_id
on policy_rag.policy_chunks (corpus_id);

create index if not exists idx_policy_chunks_policy_version
on policy_rag.policy_chunks (corpus_id, policy_id, policy_version);

create index if not exists idx_policy_chunks_jurisdiction
on policy_rag.policy_chunks (jurisdiction);

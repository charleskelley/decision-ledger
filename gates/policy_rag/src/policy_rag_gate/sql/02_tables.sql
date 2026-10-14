-- =============================================================================
-- RAG policy gate — table definitions
-- Depends on 01_init.sql (vector extension + policy_rag schema).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- policy_chunks — pgvector HNSW index for dense policy retrieval
--
-- Each row is one paragraph-level chunk from a reasoner's policy corpus.
-- Reasoners are tenants of the gate: the retriever filters by reasoner_id so
-- each reasoner sees only its own corpus. The embedding column
-- dimensionality must match the sentence-transformer model in use. Current
-- model: all-MiniLM-L6-v2 → 384 dimensions. Update vector(384) here and the
-- model in the plugin and the corpus loader together if the model changes.
--
-- risk_tier is nullable: null means the chunk applies to all tiers.
-- Populated chunks are filtered by reasoner_id, jurisdiction, and risk_tier
-- at retrieval time before the HNSW index is queried.
--
-- title is the owning document's human-readable title, denormalized onto
-- every chunk so the retriever can build a citation without reading the
-- corpus directory.
-- ---------------------------------------------------------------------------
create table if not exists policy_rag.policy_chunks (
    chunk_id uuid primary key default gen_random_uuid(),
    reasoner_id text not null,
    policy_id text not null,
    policy_version text not null,
    title text not null default '',
    jurisdiction text not null,
    risk_tier text,
    section_path text not null,
    chunk_text text not null,
    embedding vector(384) not null,
    created_at timestamptz not null default now()
);

create index if not exists idx_policy_chunks_hnsw
on policy_rag.policy_chunks
using hnsw (embedding vector_cosine_ops);

create index if not exists idx_policy_chunks_reasoner_id
on policy_rag.policy_chunks (reasoner_id);

create index if not exists idx_policy_chunks_policy_version
on policy_rag.policy_chunks (policy_id, policy_version);

create index if not exists idx_policy_chunks_jurisdiction
on policy_rag.policy_chunks (jurisdiction);

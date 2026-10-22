-- =============================================================================
-- RAG policy gate — PostgreSQL initialization (gate-owned DDL)
-- Applied idempotently by the reference retrieval provider when it is built
-- and by the corpus loader before every load (policy_rag_gate/schema.py).
--
-- The gate keeps its tables in its own schema, `policy_rag`, inside the
-- framework's database. The framework's DDL creates nothing for the gate,
-- and nothing here touches the framework's tables.
-- =============================================================================

-- pgvector for the chunk embeddings (HNSW cosine index). The framework needs
-- no extension; this gate is the only consumer.
create extension if not exists vector;

-- Gate-owned schema. All SQL in the gate schema-qualifies its tables
-- (policy_rag.<table>) so nothing depends on the per-session search_path.
create schema if not exists policy_rag;

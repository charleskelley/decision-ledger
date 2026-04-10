-- =============================================================================
-- ATO Reasoner PostgreSQL initialization
-- Runs once at container creation via docker-entrypoint-initdb.d/
-- =============================================================================

-- Enable pgvector for dense vector operations (HNSW index on policy embeddings)
create extension if not exists vector;

-- Project schema — all ATO Reasoner tables live here
create schema if not exists account_takeover;


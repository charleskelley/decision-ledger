-- =============================================================================
-- DecisionLedger PostgreSQL initialization
-- Runs once at container creation via docker-entrypoint-initdb.d/
--
-- The framework owns a single schema (`decisionledger`). All reasoners write
-- to the same tables; tenant separation is via the `reasoner_id` column.
-- =============================================================================

-- Enable pgvector for dense vector operations (HNSW index on policy embeddings)
create extension if not exists vector;

-- Framework-owned schema — all DecisionLedger tables live here.
-- All SQL in app/* schema-qualifies tables (decisionledger.<table>) so we
-- don't depend on the per-session search_path.
create schema if not exists decisionledger;

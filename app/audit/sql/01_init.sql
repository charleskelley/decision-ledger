-- =============================================================================
-- DecisionLedger PostgreSQL initialization
-- Runs once at container creation via docker-entrypoint-initdb.d/
--
-- The framework owns a single schema (`decisionledger`). All reasoners write
-- to the same tables; tenant separation is via the `reasoner_id` column.
-- The framework needs no Postgres extension: a gate that needs one (for
-- example for embeddings) creates it in its own DDL, next to its own schema.
-- =============================================================================

-- Framework-owned schema — all DecisionLedger tables live here.
-- All SQL in app/* schema-qualifies tables (decisionledger.<table>) so we
-- don't depend on the per-session search_path.
create schema if not exists decisionledger;

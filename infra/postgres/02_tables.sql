-- =============================================================================
-- DecisionLedger — initial table definitions
-- Depends on 01_init.sql (pgvector extension + decisionledger schema)
--
-- All tables live in the framework-owned `decisionledger` schema. Reasoners
-- are tenants distinguished by the `reasoner_id` column on row-level. The
-- policy gate, retrieval corpus, and audit ledger are shared resources.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- decision_bundles — append-only audit ledger for all reasoners
--
-- The full DecisionBundle is stored as JSONB so every field is queryable
-- without schema migrations as the bundle evolves. The scalar columns
-- (reasoner_id, entity_id, created_at, decision_action) are surfaced
-- top-level for indexed lookups, the replay endpoint, and tenant filtering.
--
-- decision_action is the action this decision produced at decision time —
-- immutable. For non-terminal actions (CHALLENGE, HOLD), the realized
-- outcome is recorded later in decision_resolution_attempts; this column
-- stays as written.
--
-- reasoner_id: tenant column (e.g., "ato-reasoner"). Reasoner-specific
--              business keys (account_id, content_id, …) live inside the
--              JSONB bundle, not as scalar columns.
-- entity_id:   framework UUID derived deterministically from the
--              reasoner's domain business key via UUID5.
-- ---------------------------------------------------------------------------
create table if not exists decisionledger.decision_bundles (
    decision_id uuid primary key,
    reasoner_id text not null,
    entity_id uuid not null,
    created_at timestamptz not null,
    decision_action text not null,
    bundle jsonb not null
);

create index if not exists idx_decision_bundles_reasoner_id
on decisionledger.decision_bundles (reasoner_id);

create index if not exists idx_decision_bundles_reasoner_entity
on decisionledger.decision_bundles (reasoner_id, entity_id);

create index if not exists idx_decision_bundles_entity_id
on decisionledger.decision_bundles (entity_id);

create index if not exists idx_decision_bundles_created_at
on decisionledger.decision_bundles (created_at desc);

create index if not exists idx_decision_bundles_decision_action
on decisionledger.decision_bundles (decision_action);

-- ---------------------------------------------------------------------------
-- policy_chunks — pgvector HNSW index for dense policy retrieval (shared)
--
-- Each row is one paragraph-level chunk from a reasoner's policy corpus.
-- The framework retriever filters by reasoner_id so each reasoner sees
-- only its own corpus. The embedding column dimensionality must match the
-- sentence-transformer model in use. Current model: all-MiniLM-L6-v2 →
-- 384 dimensions. Update vector(384) here and in the corpus loader if
-- the model changes.
--
-- risk_tier is nullable: null means the chunk applies to all tiers.
-- Populated chunks are filtered by reasoner_id, jurisdiction, and risk_tier
-- at retrieval time before the HNSW index is queried.
-- ---------------------------------------------------------------------------
create table if not exists decisionledger.policy_chunks (
    chunk_id uuid primary key default gen_random_uuid(),
    reasoner_id text not null,
    policy_id text not null,
    policy_version text not null,
    jurisdiction text not null,
    risk_tier text,
    section_path text not null,
    chunk_text text not null,
    embedding vector(384) not null,
    created_at timestamptz not null default now()
);

create index if not exists idx_policy_chunks_hnsw
on decisionledger.policy_chunks
using hnsw (embedding vector_cosine_ops);

create index if not exists idx_policy_chunks_reasoner_id
on decisionledger.policy_chunks (reasoner_id);

create index if not exists idx_policy_chunks_policy_version
on decisionledger.policy_chunks (policy_id, policy_version);

create index if not exists idx_policy_chunks_jurisdiction
on decisionledger.policy_chunks (jurisdiction);

-- ---------------------------------------------------------------------------
-- replay_logs — deterministic enforcement replay audit trail
--
-- One row per replay invocation. is_matched = true means re-executing
-- enforcement against the cached policy_gate_output produced the same
-- decision_action as the original decision. diff is populated only when
-- is_matched = false and contains the action discrepancy for investigation.
-- Reasoner identity is reachable via the decision_id join — no column.
-- ---------------------------------------------------------------------------
create table if not exists decisionledger.replay_logs (
    replay_id uuid primary key default gen_random_uuid(),
    decision_id uuid not null references decisionledger.decision_bundles (
        decision_id
    ),
    replayed_at timestamptz not null default now(),
    is_matched boolean not null,
    diff jsonb
);

create index if not exists idx_replay_logs_decision_id
on decisionledger.replay_logs (decision_id);

-- ---------------------------------------------------------------------------
-- decision_resolution_attempts — append-only log of resolution attempts
-- against non-terminal DecisionBundles (CHALLENGE / HOLD).
--
-- Each row is immutable. Multiple rows per decision_id are ordered by
-- (decision_id, attempt_sequence). The realized action of a decision is
-- computed at read time by folding the attempt chain — never stored.
-- Reasoner identity is reachable via the decision_id join — no column.
--
-- Per DR-21, the typed-subclass payload (one of HumanResolutionAttempt,
-- SlaDefaultResolutionAttempt, …) is stored verbatim in the `payload`
-- JSONB column. Scalar columns (resolver_kind, status, resolution_action)
-- are denormalized for indexed queries. The `evidence` column is retained
-- nullable for backward-compatible reads of pre-DR-21 rows; new writes
-- leave it null, and a follow-up migration drops it.
--
-- resolution_action is nullable: it carries an action only when an attempt
-- has produced one (status = COMPLETED, or terminal action via ESCALATED
-- intermediate paths). PENDING / EXPIRED attempts may have null action.
-- ---------------------------------------------------------------------------
create table if not exists decisionledger.decision_resolution_attempts (
    attempt_id uuid primary key,
    decision_id uuid not null references decisionledger.decision_bundles (
        decision_id
    ),
    attempt_sequence integer not null,
    started_at timestamptz not null,
    completed_at timestamptz,
    resolver_kind text not null,
    resolver_id text not null,
    status text not null,
    resolution_action text,
    note text not null,
    evidence jsonb,
    payload jsonb not null,
    unique (decision_id, attempt_sequence)
);

create index if not exists idx_resolution_attempts_decision_id
on decisionledger.decision_resolution_attempts (decision_id);

create index if not exists idx_resolution_attempts_status
on decisionledger.decision_resolution_attempts (status);

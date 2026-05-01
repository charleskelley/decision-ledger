-- =============================================================================
-- ATO Reasoner — initial table definitions
-- Depends on 01_init.sql (pgvector extension + account_takeover schema)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- decision_bundles — append-only audit store for all pipeline decisions
--
-- The full DecisionBundle is stored as JSONB so every field is queryable
-- without schema migrations as the bundle evolves. The scalar columns
-- (entity_id, account_id, created_at, decision_action) are surfaced
-- top-level for indexed lookups and the replay endpoint.
--
-- decision_action is the action this decision produced at decision time —
-- immutable. For non-terminal actions (CHALLENGE, HOLD), the realized
-- outcome is recorded later in decision_resolution_attempts; this column
-- stays as written.
--
-- entity_id:  framework UUID derived from account_id via UUID5 — use for
--             cross-domain or framework-level queries
-- account_id: ATO domain business key — use for operational queries
-- ---------------------------------------------------------------------------
create table if not exists account_takeover.decision_bundles (
    decision_id     uuid        primary key,
    entity_id       uuid        not null,
    account_id      text        not null,
    created_at      timestamptz not null,
    decision_action text        not null,
    bundle          jsonb       not null
);

create index if not exists idx_decision_bundles_entity_id
    on account_takeover.decision_bundles (entity_id);

create index if not exists idx_decision_bundles_account_id
    on account_takeover.decision_bundles (account_id);

create index if not exists idx_decision_bundles_created_at
    on account_takeover.decision_bundles (created_at desc);

create index if not exists idx_decision_bundles_decision_action
    on account_takeover.decision_bundles (decision_action);

-- ---------------------------------------------------------------------------
-- policy_chunks — pgvector HNSW index for dense policy retrieval
--
-- Each row is one paragraph-level chunk from the policy corpus. The embedding
-- column dimensionality must match the sentence-transformer model in use.
-- Current model: all-MiniLM-L6-v2 → 384 dimensions.
-- Update vector(384) here and in the corpus loader if the model changes.
--
-- risk_tier is nullable: NULL means the chunk applies to all tiers.
-- Populated chunks are filtered by jurisdiction and risk_tier at retrieval
-- time before the HNSW index is queried.
-- ---------------------------------------------------------------------------
create table if not exists account_takeover.policy_chunks (
    chunk_id      uuid        primary key default gen_random_uuid(),
    policy_id     text        not null,
    version       text        not null,
    jurisdiction  text        not null,
    risk_tier     text,
    section_path  text        not null,
    text          text        not null,
    embedding     vector(384) not null,
    created_at    timestamptz not null default now()
);

create index if not exists idx_policy_chunks_hnsw
    on account_takeover.policy_chunks
    using hnsw (embedding vector_cosine_ops);

create index if not exists idx_policy_chunks_policy_version
    on account_takeover.policy_chunks (policy_id, version);

create index if not exists idx_policy_chunks_jurisdiction
    on account_takeover.policy_chunks (jurisdiction);

-- ---------------------------------------------------------------------------
-- replay_logs — deterministic enforcement replay audit trail
--
-- One row per replay invocation. matched = true means re-executing enforcement
-- against the cached policy_gate_output produced the same decision_action as
-- the original decision. diff is populated only when matched = false and
-- contains the action discrepancy for investigation.
-- ---------------------------------------------------------------------------
create table if not exists account_takeover.replay_logs (
    replay_id    uuid        primary key default gen_random_uuid(),
    decision_id  uuid        not null
                             references account_takeover.decision_bundles (decision_id),
    replayed_at  timestamptz not null default now(),
    matched      boolean     not null,
    diff         jsonb
);

create index if not exists idx_replay_logs_decision_id
    on account_takeover.replay_logs (decision_id);

-- ---------------------------------------------------------------------------
-- decision_resolution_attempts — append-only log of resolution attempts
-- against non-terminal DecisionBundles (CHALLENGE / HOLD).
--
-- Each row is immutable. Multiple rows per decision_id are ordered by
-- (decision_id, sequence). The realized action of a decision is computed at
-- read time by folding the attempt chain — never stored.
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
create table if not exists account_takeover.decision_resolution_attempts (
    attempt_id        uuid        primary key,
    decision_id       uuid        not null
                                  references account_takeover.decision_bundles (decision_id),
    sequence          integer     not null,
    started_at        timestamptz not null,
    completed_at      timestamptz,
    resolver_kind     text        not null,
    resolver_id       text        not null,
    status            text        not null,
    resolution_action text,
    note              text        not null,
    evidence          jsonb,
    payload           jsonb       not null,
    unique (decision_id, sequence)
);

create index if not exists idx_resolution_attempts_decision_id
    on account_takeover.decision_resolution_attempts (decision_id);

create index if not exists idx_resolution_attempts_status
    on account_takeover.decision_resolution_attempts (status);

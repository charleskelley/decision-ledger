-- =============================================================================
-- DecisionLedger — initial table definitions
-- Depends on 01_init.sql (decisionledger schema)
--
-- All tables live in the framework-owned `decisionledger` schema. Reasoners
-- are tenants distinguished by the `reasoner_id` column on row-level. These
-- are the audit ledger's tables only: decision bundles, replay logs,
-- resolution attempts, and the reasoner registration snapshots bundles cite.
-- Gates keep whatever they store in their own schemas, created by their own
-- DDL.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- decision_bundles — append-only audit ledger for all reasoners
--
-- The full DecisionBundle is stored as JSONB so every field is queryable
-- without schema migrations as the bundle evolves. The scalar columns
-- (reasoner_id, entity_id, created_at, decision_action, schema_version) are
-- surfaced top-level for indexed lookups, the replay endpoint, and tenant
-- filtering.
--
-- decision_action is the action this decision produced at decision time —
-- immutable. For non-terminal actions (CHALLENGE, HOLD), the realized
-- outcome is recorded later in decision_resolution_attempts; this column
-- stays as written.
--
-- reasoner_id: tenant column holding the composing reasoner's id. A
--              reasoner's own business keys live inside the JSONB bundle,
--              not as scalar columns.
-- entity_id:   framework UUID derived deterministically from the
--              reasoner's domain business key via UUID5.
-- schema_version: shape of the JSONB bundle. 2 = the submission contract
--              (the reasoner's Submission and one record per gate). 1 =
--              rows written before it: lookup returns them as stored;
--              typed loading and replay refuse them (HTTP 409
--              BUNDLE_SCHEMA_UNSUPPORTED).
-- ---------------------------------------------------------------------------
create table if not exists decisionledger.decision_bundles (
    decision_id uuid primary key,
    reasoner_id text not null,
    entity_id uuid not null,
    created_at timestamptz not null,
    decision_action text not null,
    schema_version integer not null,
    bundle jsonb not null
);

-- A table created before schema version 2 lacks the column: add it once,
-- marking every existing row as version 1, then drop the default so fresh
-- and upgraded tables end up identical (bar column order) and every write
-- states its version. Guarded, because ALTER TABLE takes an ACCESS EXCLUSIVE
-- lock even when it would change nothing, and this file runs on every
-- startup: an unguarded no-op would queue behind any open reader.
do $$
begin
    if not exists (
        select 1
        from information_schema.columns
        where
            table_schema = 'decisionledger'
            and table_name = 'decision_bundles'
            and column_name = 'schema_version'
    ) then
        alter table decisionledger.decision_bundles
        add column schema_version integer not null default 1;
        alter table decisionledger.decision_bundles
        alter column schema_version drop default;
    end if;
end
$$;

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
-- replay_logs — deterministic enforcement replay audit trail
--
-- One row per replay invocation. is_matched = true means re-executing
-- enforcement against the cached gate output produced the same
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

-- ---------------------------------------------------------------------------
-- reasoner_registrations — every registration version a decision can cite
--
-- A registration is data (DR-31). Its version is the content address of its
-- canonical JSON (core.observation.registration_version); every bundle
-- records the version in effect when it was decided
-- (bundle ->> 'registration_version'), and replay reads that version's
-- snapshot here, so a decision is re-enforced against the profile it was
-- made under even after its registration changed or its reasoner left the
-- deployment. A row is written the first time a deployment serves that
-- version (insert ... on conflict do nothing) and is never updated.
-- ---------------------------------------------------------------------------
create table if not exists decisionledger.reasoner_registrations (
    reasoner_id text not null,
    registration_version text not null,
    registration jsonb not null,
    first_registered_at timestamptz not null default now(),
    primary key (reasoner_id, registration_version)
);

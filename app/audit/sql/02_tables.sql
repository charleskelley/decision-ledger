-- =============================================================================
-- DecisionLedger — initial table definitions
-- Depends on 01_init.sql (decisionledger schema)
--
-- All tables live in the framework-owned `decisionledger` schema. Reasoners
-- are tenants distinguished by the `reasoner_id` column on row-level. These
-- are the audit ledger's tables only: decision bundles, replay logs,
-- resolution attempts, the reasoner registration snapshots bundles cite, the
-- registrations written over the API (DR-33), and the identity journal and
-- its membership projection (DR-32). Gates keep whatever they store in their
-- own schemas, created by their own DDL.
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
-- entity_id:   the decision's subject: the identity the ledger resolved
--              before any gate ran (bundle -> 'identity', DR-32), the
--              reasoner's own entity_id when it is unresolved or the row
--              predates the identity contract.
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

-- ---------------------------------------------------------------------------
-- reasoner_registry — the registrations written over the API (DR-33)
--
-- One row per reasoner registered with PUT /api/v1/reasoners/{reasoner_id}:
-- the version the ledger serves now (its registration is that version's
-- snapshot in reasoner_registrations), its source ('api'), who wrote that
-- version (a principal label), when the reasoner was first registered, and
-- when its current version was written. The table is the source of truth on
-- restart: the ledger composes these rows with its plugins' registrations
-- and its registration file's, and refuses to start when one reasoner_id
-- comes from two sources. The first PUT inserts the row and each PUT that
-- changes the registration updates it, in the transaction that stores the
-- new version's snapshot; nothing deletes a row (deregistration is deferred:
-- bundles reference registrations).
-- ---------------------------------------------------------------------------
create table if not exists decisionledger.reasoner_registry (
    reasoner_id text primary key,
    registration_version text not null,
    source text not null,
    registered_by text not null,
    registered_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint reasoner_registry_source_api check (source = 'api'),
    constraint reasoner_registry_version_snapshotted foreign key (
        reasoner_id, registration_version
    ) references decisionledger.reasoner_registrations (
        reasoner_id, registration_version
    )
);

-- ---------------------------------------------------------------------------
-- entity_events — the identity journal (DR-32), append-only
--
-- Every change to the identity graph, in append order (event_sequence): merge
-- (two ids are one subject; subject_entity_id is the id kept for display), detach
-- (a merge of the two ids ends, in either order), relate and unrelate (a typed
-- relationship from subject to object begins or ends). Bundles never change:
-- a decision keeps the identity it was made under, and the journal records
-- what changed since. Rows are never updated or deleted; a correction is a new
-- event. The ids are framework UUIDs only; native keys stay in the bundles.
-- provenance holds who appended the event and from what (core.identity.
-- JournalProvenance).
-- ---------------------------------------------------------------------------
create table if not exists decisionledger.entity_events (
    event_id uuid primary key,
    event_sequence bigint generated always as identity,
    event_type text not null,
    subject_entity_id uuid not null,
    object_entity_id uuid not null,
    relationship_type text,
    effective_at timestamptz not null,
    recorded_at timestamptz not null default now(),
    provenance jsonb not null,
    constraint entity_events_event_type_known check (
        event_type in ('merge', 'detach', 'relate', 'unrelate')
    ),
    constraint entity_events_relationship_typed check (
        (event_type in ('relate', 'unrelate')) = (relationship_type is not null)
    ),
    constraint entity_events_links_two_ids check (
        subject_entity_id <> object_entity_id
    )
);

create unique index if not exists idx_entity_events_sequence
on decisionledger.entity_events (event_sequence);

create index if not exists idx_entity_events_subject
on decisionledger.entity_events (subject_entity_id);

create index if not exists idx_entity_events_object
on decisionledger.entity_events (object_entity_id);

-- ---------------------------------------------------------------------------
-- entity_membership — the membership projection (DR-32), rebuildable
--
-- Derived, never authoritative: the fold of the journal and of every
-- relationship a decision's resolved identity asserted (bundle -> 'identity'
-- -> 'related'), as validity intervals (core.identity.project_membership is
-- its definition; the ledger rebuilds it from those two sources and keeps it
-- current as decisions are written). relationship_type is null for a merge:
-- related_entity_id is then the same subject as subject_entity_id. valid_to is
-- null while the membership holds. A subject's timeline unions the ids merged
-- with it (and, on request, the subjects one relationship of a type away) by
-- reading the rows that hold now; the bundle itself says who belonged with the
-- subject as of the decision.
-- ---------------------------------------------------------------------------
create table if not exists decisionledger.entity_membership (
    subject_entity_id uuid not null,
    related_entity_id uuid not null,
    relationship_type text,
    valid_from timestamptz not null,
    valid_to timestamptz,
    constraint entity_membership_interval_ordered check (
        valid_to is null or valid_to > valid_from
    ),
    constraint entity_membership_links_two_ids check (
        subject_entity_id <> related_entity_id
    )
);

-- At most one open interval per link, so recording a relationship that already
-- holds is a no-op (insert ... on conflict do nothing).
create unique index if not exists idx_entity_membership_open_link
on decisionledger.entity_membership (
    subject_entity_id, related_entity_id, relationship_type
) nulls not distinct
where valid_to is null;

create index if not exists idx_entity_membership_subject
on decisionledger.entity_membership (subject_entity_id)
where valid_to is null;

create index if not exists idx_entity_membership_related
on decisionledger.entity_membership (related_entity_id)
where valid_to is null;

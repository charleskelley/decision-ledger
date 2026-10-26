# Infrastructure

The local development stack, the database schema, the stores that gates and
reasoners own, the production deployment model, and the environment
variables.

The framework's only infrastructure is PostgreSQL. The ledger keeps the
Decision Bundles, replay logs, resolution attempts, registration snapshots,
registrations written over the API, evidence escrow, and identity journal
in one schema, `decisionledger`, and needs nothing else to run. A gate or a
reasoner that needs more (a search index, a feature store, a queue) brings
it in its own compose file and configures it under its own prefix: the
framework never connects to it.

---

## Local Development Stack

All infrastructure runs locally with Docker Compose V2. `docker compose up -d`
brings up the full stack; no cloud account is needed for development or
testing.

The root `docker-compose.yml` defines one service, `postgres`, the framework's.
The infrastructure a plugin needs for itself lives in that plugin's own
compose file and joins the same stack through a top-level `include:` (Compose
2.20 or later):

```yaml
include:
  - reasoners/account_takeover/compose.yaml   # Redis, the ATO reasoner's
  - gates/policy_rag/compose.yaml             # Elasticsearch, the RAG policy gate's
```

```bash
docker compose up -d        # Start all services in background
docker compose down         # Stop services (volumes preserved)
docker compose down -v      # Full reset: removes all volumes and data
docker compose logs -f      # Tail all logs
```

### Services

| Service | Owner (compose file) | Image | Port | Purpose |
|---------|----------------------|-------|------|---------|
| `postgres` | Framework (`docker-compose.yml`) | `pgvector/pgvector:pg16` | 5432 (`POSTGRES_PORT`) | The ledger's `decisionledger` schema, shared by every reasoner; also hosts the RAG policy gate's own `policy_rag` schema |
| `elasticsearch` | RAG policy gate (`gates/policy_rag/compose.yaml`) | `docker.elastic.co/elasticsearch/elasticsearch:8.13.4` | 9200 (`POLICY_RAG_GATE_ELASTICSEARCH_PORT`) | The gate's BM25 index over every corpus its reference provider holds |
| `redis` | ATO reasoner (`reasoners/account_takeover/compose.yaml`) | `redis:7-alpine` | 6379 (`ATO_REDIS_PORT`) | ATO's login-event stream, dedup store, and feature windows |

**Why the Postgres image is `pgvector/pgvector:pg16`.** It is plain Postgres 16
plus the `vector` extension, available for a gate's DDL to create. The
framework's own DDL creates no extension. The RAG policy gate keeps its dense
retrieval chunks on the same server, in its own schema, and its DDL creates
the extension there (see [Gate-owned storage](#gate-owned-storage)).

Nothing in the framework (`core/`, `app/`, `eval/`) uses Redis or
Elasticsearch. ATO opens its own Redis client from `ATO_REDIS_URL`
(`ato_reasoner/plugin.py`); the RAG policy gate's reference retrieval
provider opens its own Elasticsearch client from
`POLICY_RAG_GATE_ELASTICSEARCH_URL` the first time a binding names it (when
the gate admits the binding, at startup or on a registration write).

The services that run Python are not containerized yet. They run directly with
uv during development:

| Process | Command | Port |
|---------|---------|------|
| The ledger API | `make serve` (`uv run uvicorn --factory app.main:create_app`) | 8000 |
| ATO's own service | `make serve-account_takeover` (`ato_reasoner.app:create_app`) | 8001 |
| The stream mock's own service | `make serve-mock_stream` (`mock_stream_reasoner.app:create_app`) | 8002 |
| The MCP server | `uv run decisionledger-mcp` (stdio) | none |

ATO's service submits to the ledger in its own process through `LocalClient`
(writing to the same Postgres) unless `ATO_LEDGER_URL` points it at a ledger
over HTTP. The stream mock's service always submits over HTTP, to
`MOCK_STREAM_LEDGER_URL`. The [local runbook](../development/runbook-local.md)
walks through a first run.

### Resource Expectations (M1 Max, 64GB RAM)

The full stack (Redis, PostgreSQL with pgvector, and Elasticsearch) is
validated on an M1 Max 64GB. Elasticsearch is the most resource-intensive
service; `gates/policy_rag/compose.yaml` sets `ES_JAVA_OPTS=-Xms512m -Xmx512m`
for development. Security is disabled for the local Elasticsearch service
(`xpack.security.enabled=false`): never expose it to a public network.

---

## Database Schema

### PostgreSQL schema: `decisionledger` (framework)

Every framework table lives in the `decisionledger` schema and is shared by
every reasoner; tenant separation is the `reasoner_id` column, on the tables
that have one, and the `decision_id` join on the rest (DR-24). The one
exception is `evidence_artifacts`: its rows are deployment-wide, shared by
content across reasoners (two reasoners' decisions that rely on the same
document cite one row), so retention cannot be scoped to one reasoner
through `first_decision_id`. The canonical
DDL ships inside the framework package: `app/audit/sql/01_init.sql` creates
the schema (and no extension), and `app/audit/sql/02_tables.sql` creates every
table and index. Both are idempotent (`create ... if not exists`; the one
upgrade statement is guarded) and are applied three ways:

- Compose mounts them into `/docker-entrypoint-initdb.d/`, so a fresh Postgres
  volume initializes on first start.
- `app/audit/schema.py:apply_canonical_schema` runs them every time the
  framework's services are built (`app/bootstrap.py:build_framework_services`,
  which the API's startup, `LocalClient.from_settings`, and each reasoner's
  eval driver call).
- The CI integration and eval workflows apply them with `psql` against the
  service container.

`02_tables.sql` is authoritative; the summaries below omit most indexes.

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `decision_bundles` | The append-only audit ledger: one row per decision, every reasoner | `decision_id` (PK), `reasoner_id`, `entity_id`, `created_at`, `decision_action`, `schema_version`, `bundle` (JSONB) |
| `replay_logs` | One row per replay of a decision's enforcement | `replay_id`, `decision_id` (FK), `replayed_at`, `is_matched`, `diff` |
| `decision_resolution_attempts` | The append-only log of resolution attempts on held or challenged decisions (DR-18, DR-21) | `attempt_id`, `decision_id` (FK), `attempt_sequence` (unique per decision), `resolver_kind`, `resolver_id`, `status`, `resolution_action`, `payload` (JSONB) |
| `reasoner_registrations` | Every registration version a bundle can cite (DR-31) | `reasoner_id`, `registration_version` (PK together), `registration` (JSONB), `first_registered_at` |
| `reasoner_registry` | The registrations written over the API, as served now (DR-33; see [Registration](registration.md)) | `reasoner_id` (PK), `registration_version` (FK to `reasoner_registrations`), `source` (`api`), `registered_by`, `registered_at`, `updated_at` |
| `entity_events` | The identity journal, append-only (DR-32) | `event_id` (PK), `event_sequence`, `event_type`, `subject_entity_id`, `object_entity_id`, `relationship_type`, `effective_at`, `recorded_at`, `provenance` (JSONB) |
| `entity_membership` | The membership projection, rebuildable (DR-32) | `subject_entity_id`, `related_entity_id`, `relationship_type`, `valid_from`, `valid_to` |
| `evidence_artifacts` | The evidence escrow: the content gates relied on, content-addressed (DR-34) | `artifact_hash` (PK), `media_type`, `byte_length`, `artifact_bytes` (bytea), `first_seen_at`, `first_decision_id`, `first_gate_id` |

#### `decision_bundles`

```sql
create table if not exists decisionledger.decision_bundles (
    decision_id uuid primary key,
    reasoner_id text not null,
    entity_id uuid not null,
    created_at timestamptz not null,
    decision_action text not null,
    schema_version integer not null,
    bundle jsonb not null
);
```

- `bundle` is the full `DecisionBundle`; the scalar columns are surfaced for
  indexed lookups, replay, tenant filtering, and the subject timeline.
- `reasoner_id` is the tenant column. A reasoner's own business keys (ATO's
  `account_id`, for example) live inside the JSONB bundle, never as columns.
- `entity_id` is the decision's subject as the ledger resolved it before any
  gate ran (`bundle -> 'identity'`, DR-32); it is the reasoner's own
  `entity_id` when the subject is unresolved under it, or when the row
  predates the identity contract. See [Identity](identity.md).
- `decision_action` is the action the decision produced, immutable. The
  realized outcome of a CHALLENGE or HOLD is folded from
  `decision_resolution_attempts` at read time.
- `schema_version` is the bundle's shape. `2` is the submission contract (the
  reasoner's `Submission` and one record per gate). `1` marks rows written
  before it: lookup returns them as stored, and typed loading and replay
  refuse them (`409 BUNDLE_SCHEMA_UNSUPPORTED`). A table created before the
  column existed gains it once, with every existing row marked `1`.

Replay reads no reasoner code and no corpus: it re-enforces the bundle's
stored gate outputs against the snapshot of the registration version the
bundle cites (`reasoner_registrations`). A row with no registration version,
whose reasoner the deployment no longer registers, answers
`409 REASONER_NOT_COMPOSED`; a cited version with no snapshot answers
`409 REGISTRATION_SNAPSHOT_MISSING`.

#### `replay_logs` and `decision_resolution_attempts`

Neither carries `reasoner_id`; both reach it through the `decision_id`
foreign key. `replay_logs.diff` is populated only when `is_matched = false`.
Resolution attempts are immutable rows ordered by `(decision_id,
attempt_sequence)`; the typed attempt is stored verbatim in `payload`, with
`resolver_kind`, `status`, and `resolution_action` denormalized for queries.
The nullable `evidence` column is kept for reads of rows written before DR-21;
new writes leave it null.

#### `reasoner_registrations`

A registration is data (DR-31). Its version is the content address of its
canonical JSON (`core.observation.registration_version`). A row is written the
first time a deployment serves that version (`insert ... on conflict do
nothing`) and is never updated, so a decision replays against the profile it
was made under even after its registration changed or its reasoner left the
deployment.

#### `evidence_artifacts`

```sql
create table if not exists decisionledger.evidence_artifacts (
    artifact_hash text primary key,     -- SHA-256 of artifact_bytes, lowercase hex
    media_type text not null,
    byte_length bigint not null,
    artifact_bytes bytea not null,
    first_seen_at timestamptz not null default now(),
    first_decision_id text not null,    -- not a foreign key
    first_gate_id text not null
);
```

The evidence escrow (DR-34). A gate that keeps a copy of what it relied on
(the RAG gate: each cited document, whole; the rules gate: the rule set it
applied) hands the bytes to `app.audit.ArtifactStore.put` while it decides;
the store writes one row per distinct content (`insert ... on conflict do
nothing`) and returns the digest, which the gate records as the evidence
reference's `artifact_hash`. The excerpt omits the check constraints: the
key is a 64-character hex digest, it is the SHA-256 of `artifact_bytes`
(the database pins the content address), and `byte_length` is their
length. `first_decision_id` names the decision that first escrowed the
content, as the gate received it; it is not a foreign key, since a gate
escrows before its decision's bundle is written. A row is shared by every
decision, of any reasoner, that relied on the same content. Rows are never
updated, and nothing in the framework deletes one: retention is the
deployment's records rule. Verify (`POST /api/v1/decisions/{decision_id}/verify`) reads
which artifacts are held (`ArtifactStore.held`), and a gate verifying its
references reads its own back (`get`). With
`DECISIONLEDGER_EVIDENCE_ESCROW=off` nothing is written; rows written
earlier stay readable.

#### `entity_events` and `entity_membership`

```sql
create table if not exists decisionledger.entity_events (
    event_id uuid primary key,
    event_sequence bigint generated always as identity,
    event_type text not null,          -- merge | detach | relate | unrelate
    subject_entity_id uuid not null,
    object_entity_id uuid not null,
    relationship_type text,            -- set exactly on relate and unrelate
    effective_at timestamptz not null,
    recorded_at timestamptz not null default now(),
    provenance jsonb not null          -- core.identity.JournalProvenance
);

create table if not exists decisionledger.entity_membership (
    subject_entity_id uuid not null,
    related_entity_id uuid not null,
    relationship_type text,            -- null for a merge
    valid_from timestamptz not null,
    valid_to timestamptz               -- null while the membership holds
);
```

The excerpt omits the check constraints. `entity_events` checks the event
type, that `relationship_type` is set exactly on `relate` and `unrelate`, and
that the two ids differ; `entity_membership` checks that an interval ends
after it begins and links two different ids.

- `entity_events` is the identity journal. `event_sequence` orders appends;
  rows are never updated or deleted, and the ids are framework UUIDs only
  (native keys stay in the bundles).
- `entity_membership` is derived, never authoritative: the fold of the
  journal and of every relationship a decision's identity asserted
  (`core.identity.project_membership`). A partial unique index allows at most
  one open interval per link (`nulls not distinct`, so merges count too),
  which makes re-asserting a relationship that already holds a no-op.

Writing a bundle opens the relationships its identity asserts;
`app.audit.EntityJournal` appends journal events and rebuilds the projection.
[Identity](identity.md#journal-and-projection) describes the fold and the
journal's use.

### Gate-owned storage

Gates own their infrastructure (DR-28). What follows belongs to the **RAG
policy gate** (`gates/policy_rag`, package `policy_rag_gate`); the framework's
DDL creates none of it, and the gate's DDL touches nothing of the framework's.
The rules gate (`gates/rules`) owns no storage. Both gates escrow what they
rely on in the framework's `evidence_artifacts`, through the framework's
store, never through DDL or SQL of their own.

#### PostgreSQL schema: `policy_rag`

The gate's DDL ships in its package (`policy_rag_gate/sql/01_init.sql` and
`02_tables.sql`): the stores of its reference retrieval provider (DR-34).
`01_init.sql` creates the `vector` extension (the connecting role must be
allowed to create it) and the `policy_rag` schema; `02_tables.sql` creates
`policy_rag.corpora`, `policy_rag.documents`, and `policy_rag.policy_chunks`.
`policy_rag_gate.schema.apply_schema` applies both, idempotently, every time
the reference provider is built and before every corpus load;
`make prepare-gate-policy_rag` (or `make prepare-gates`, every gate) applies
them on their own. A database that holds the chunk table from before DR-34
(keyed by `reasoner_id`) is refused before any statement runs: drop the
schema (`docker compose down -v` in development, or
`drop schema policy_rag cascade`) and reload each corpus.

```sql
create table if not exists policy_rag.corpora (
    corpus_id text primary key,            -- the id bindings name
    manifest_hash text not null,           -- sha256 over (document_id, version, content_hash)
    document_count integer not null,
    loaded_at timestamptz not null default now(),
    source_dir text not null
);

create table if not exists policy_rag.documents (
    corpus_id text not null references policy_rag.corpora (corpus_id) on delete cascade,
    document_id text not null,
    document_version text not null,
    title text not null,
    content_hash text not null,            -- sha256 of body
    body text not null,                    -- the document whole, front matter stripped
    metadata jsonb not null default '{}'::jsonb,   -- its front matter
    source_path text not null,
    primary key (corpus_id, document_id, document_version)
);

create table if not exists policy_rag.policy_chunks (
    chunk_id text primary key,             -- sha256(corpus_id, document_id, version, section_path)
    corpus_id text not null,               -- the filter applied at retrieval
    policy_id text not null,
    policy_version text not null,
    title text not null default '',        -- document title, for citations
    jurisdiction text not null,
    risk_tier text,                        -- null means applies to all tiers
    section_path text not null,
    chunk_text text not null,
    span_start integer,                    -- chunk_text's offsets in the document body,
    span_end integer,                      -- when it is a contiguous slice of it
    embedding vector(384) not null,        -- all-MiniLM-L6-v2 dimensions
    created_at timestamptz not null default now(),
    foreign key (corpus_id, policy_id, policy_version)
    references policy_rag.documents (corpus_id, document_id, document_version)
    on delete cascade
);

create index if not exists idx_policy_chunks_hnsw
on policy_rag.policy_chunks
using hnsw (embedding vector_cosine_ops);
```

The `vector(384)` dimension matches the `all-MiniLM-L6-v2` sentence-transformer
model. If the embedding model changes, update this column, the reference
provider, and the corpus loader together. `title` lets the retriever build
citations from the row alone, without reading any corpus directory.
`corpus_id` is the only key on a chunk: which corpora a reasoner reads is its
registration's binding.

#### Elasticsearch index: `policy-chunks`

One index, shared by every corpus the reference provider holds. The mapping
ships in the gate's package at `policy_rag_gate/es/policy-chunks.json`; the
corpus loader creates the index on first load if it does not exist.
Documents are written under their deterministic chunk ids, so a reload
overwrites them; `span_start` and `span_end` are stored, not indexed.

`corpus_id` is mapped as `keyword`, and the retriever applies it as a `term`
filter. An index created before DR-34 (keyed by `reasoner_id`), or one where
the field is analyzed `text` (a `term` filter on a hyphenated id matches
nothing), lacks a keyword `corpus_id`. The loader detects that and recreates
the index, logging that it did. Recreating drops every corpus's BM25
documents, so load every corpus afterward (`make build-policy-index`).

The `text` field uses a dual-analyzer multi-field strategy (DR-13):

- `text`: English analyzer with stemming, for natural-language queries.
- `text.keyword_search`: standard analyzer, for exact identifiers (section
  numbers, control ids, policy ids).

The retriever queries both fields with `multi_match`. Retrieval filters:
`corpus_id` (one query per bound corpus) and `jurisdictions` filter both
stores; `risk_tier` filters pgvector only. `reasoner_id` is not a chunk key:
which corpora a reasoner reads is its registration's binding.

---

## Redis Key Space (ATO reasoner)

Redis belongs to the ATO reasoner, and every key is prefixed `ato:`. The table
is a reference for the reasoner's own code (`ato_reasoner/ingestion/`,
`ato_reasoner/feature_service.py`, `ato_reasoner/generator/`); the framework
never reads these keys.

| Key Pattern | Type | TTL | Purpose |
|-------------|------|-----|---------|
| `ato:login-events` | Stream | none | Login-event queue (produced by the scenario generator) |
| `ato:login-events:dlq` | Stream | none | Dead-letter queue (schema errors, lateness, duplicates; `ledger_refused` and `pipeline_error` from `python -m ato_reasoner consume`) |
| `ato:dedup:<sha256>` | String | 24 h | Idempotency store: SHA-256 of `event_id`, `SET NX EX` |
| `ato:velocity:{account_id}` | Sorted Set | 48 h | Sliding-window event timestamps |
| `ato:ips:{account_id}` | Set | 90 d | Seen IP addresses |
| `ato:devices:{account_id}` | Set | 90 d | Seen device fingerprints |
| `ato:countries:{account_id}` | Set | 90 d | Seen country codes |
| `ato:ua:{account_id}` | Set | 90 d | Seen user-agent strings |
| `ato:last_geo:{account_id}` | Hash | 90 d | Last lat/lon and timestamp, for impossible-travel detection |
| `ato:total:{account_id}` | String | none | Lifetime event count |

---

## Policy Corpus Build

Each reasoner that binds the RAG policy gate names, on its binding, the
corpora the gate reads for it (DR-34); a reasoner's own corpus ships with it
(its plugin's `corpus_dir`, or under the gate's asset root), and the gate's
corpus loader loads it under the corpus id the binding names. The gate
refuses a binding to a corpus that is not loaded, so load it before the
deployment starts or the registration is written, and again whenever the
corpus changes.

```bash
# Every installed reasoner's corpus (aggregate over reasoners/*/reasoner.mk)
make build-policy-index

# One reasoner's corpus, as the corpus id its reasoner.mk declares
# (POLICY_CORPUS_ID_ato-reasoner := ato-policy)
make build-policy-index-ato-reasoner

# One reasoner: load its corpus, and (for ATO) train its model if missing
make prepare-account_takeover

# The loader directly (safe to re-run: a load replaces the corpus)
uv run python -m policy_rag_gate.corpus_loader --corpus-id ato-policy --reasoner-id ato-reasoner

# Delete the corpus's Elasticsearch documents first, then load
uv run python -m policy_rag_gate.corpus_loader --corpus-id ato-policy --reasoner-id ato-reasoner --wipe

# Explicit corpus root instead of the plugin's corpus_dir or the asset root's
uv run python -m policy_rag_gate.corpus_loader --corpus-id ato-policy --corpus-dir path/to/corpus
```

The loader:

1. Resolves the corpus directory: `--corpus-dir` if given, otherwise the
   `corpus_dir` of the composed reasoner plugin whose `reasoner_id` matches
   `--reasoner-id` (for ATO, `ato_reasoner/corpus/`), otherwise
   `<POLICY_RAG_GATE_ASSETS_ROOT>/<reasoner_id>/corpus`, and reads every
   `**/*.md` file under it. It asks no registry.
2. Parses YAML frontmatter into `PolicyDocument`. A file that does not
   parse, or whose front matter JSON cannot carry, refuses the whole load
   (a load replaces the corpus, so it would drop the document).
3. Chunks each document at `##` boundaries, with a `###` secondary split for
   sections over 300 words (rules in [the policy corpus page](gates/policy/policy-corpus.md)).
4. Embeds the chunks with `all-MiniLM-L6-v2` (384 dimensions) through
   sentence-transformers.
5. Takes a Postgres advisory lock on the corpus id (held until step 6 is
   done, so two loads of one corpus never interleave), applies the gate's
   DDL, then, in one transaction, replaces the corpus: its
   `policy_rag.corpora` row (manifest hash over every document's id,
   version, text, and front matter; document count, load time, source
   directory), its documents, and its chunks with their embeddings,
   `corpus_id`, `title`, and spans, reading the Postgres DSN from
   `FrameworkSettings`.
6. Creates the `policy-chunks` index if missing (recreating it if its
   `corpus_id` mapping is stale), writes every chunk's text and metadata (no
   embedding) under its id to the Elasticsearch at
   `POLICY_RAG_GATE_ELASTICSEARCH_URL`, and deletes the corpus's documents
   the load did not write.

Verify the load:

```bash
# PostgreSQL: chunk count per policy document
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c "SELECT policy_id, COUNT(*) AS chunks FROM policy_rag.policy_chunks GROUP BY policy_id ORDER BY chunks DESC;"

# Elasticsearch: total chunk count
curl -s localhost:9200/policy-chunks/_count | python3 -m json.tool
```

---

## Production Deployment Model (AWS)

<!-- Target architecture only; no deployment code exists in the repository yet. -->

The production deployment targets AWS. This section describes the target
architecture; the repository holds no deployment code yet. The local Docker
Compose services map to AWS managed services:

| Local | Owner | AWS Equivalent | Notes |
|-------|-------|---------------|-------|
| PostgreSQL | Framework (and the RAG policy gate's own schema) | RDS PostgreSQL or Aurora PostgreSQL | The pgvector extension is needed only where the RAG policy gate runs |
| Elasticsearch | RAG policy gate | Amazon OpenSearch Service | Same BM25 functionality |
| Redis Streams | ATO reasoner | ElastiCache for Redis | Cluster mode for partitioned streams; provisioned per reasoner that needs it |
| Ledger API (FastAPI) | Framework | ECS Fargate (or EKS) | Decides statelessly and scales horizontally; its live registry is per process (see below) |
| ATO service (FastAPI) | ATO reasoner | ECS Fargate (or EKS) | A separate service that submits to the ledger over HTTP |

### Terraform Modules (planned)

A planned layout; no `infra/` directory exists yet.

```
infra/
├── modules/
│   ├── networking/     VPC, subnets, security groups
│   ├── redis/          ElastiCache cluster
│   ├── postgres/       RDS instance + pgvector setup
│   ├── opensearch/     OpenSearch domain
│   └── ecs/            ECS cluster, task definitions, service
└── environments/
    └── staging/        Variable values for staging deployment
```

### Horizontal Scaling Considerations

The ledger API keeps every decision's state in Postgres, and the gates it
composes run inside its process, so deciding scales horizontally without
coordination. Registering over the API does not yet (DR-33): each process
holds its live registry in memory and reads the registrations written over
the API only when it starts, so a `PUT` is served by the process that took
it, and by the others once they restart.

ATO's feature service is **not** trivially scalable: it keeps sliding-window
state in Redis sorted sets keyed by `account_id`. A naive scale-out produces
correct results (Redis is the source of truth), but requires care during
graceful shutdown to avoid corrupting in-flight window state.

At production scale, ATO's Redis Streams would be replaced with Kafka or AWS
Kinesis (see DR-1 in [decisions.md](./decisions.md)). Consumer-group
partitioning by `account_id` routes all events for one account to the same
feature service instance, keeping window state local to it.

---

## Environment Configuration

All runtime configuration comes from environment variables, never from
secrets in code or committed config files. Copy `.env.example` to `.env` for
local overrides; see [Secrets](../operations/secrets.md) for how secrets reach
each deployment mode.

Each component reads its own variables. Unprefixed variables configure the
framework and are read by `FrameworkSettings` (`app/settings.py`). A gate or a
reasoner reads its own variables under its own prefix, in its own settings
class: `RULES_GATE_` (`rules_gate/settings.py`), `POLICY_RAG_GATE_`
(`policy_rag_gate/settings.py`), `ATO_` (`AtoSettings` in
`ato_reasoner/settings.py`), and `MOCK_STREAM_`, `MOCK_BATCH_`, `MOCK_TEXT_`,
and `MOCK_DAILY_` (each mock's `settings.py`). The LLM providers' keys keep
their providers' names. Port variables marked "Compose" only change a host
port mapping in a compose file.

### Framework

| Variable | Read by | Description | Default |
|----------|---------|-------------|---------|
| `POSTGRES_HOST` | Framework | PostgreSQL host | `localhost` |
| `POSTGRES_PORT` | Framework, Compose | PostgreSQL port | `5432` |
| `POSTGRES_USER` | Framework, Compose | PostgreSQL user | `decisionledger` |
| `POSTGRES_PASSWORD` | Framework, Compose | PostgreSQL password | `decisionledger` |
| `POSTGRES_DB` | Framework, Compose | PostgreSQL database | `decisionledger` |
| `LOG_JSON` | Framework | JSON log rendering | `true` |
| `LOG_LEVEL` | Framework | Log level | `INFO` |
| `DECISIONLEDGER_REASONERS` | Framework | Comma-separated allowlist of `reasoner_id`s to compose from the installed plugins (plugins only: the file's and the API's registrations are always served) | blank (every installed plugin) |
| `DECISIONLEDGER_MANDATORY_GATES` | Framework | Comma-separated `gate_id`s run on every decision of every reasoner (DR-29); each must be composed, or startup fails | blank (none) |
| `DECISIONLEDGER_REGISTRATIONS` | Framework | Path to a YAML file of reasoner registrations (source `file:<path>`), composed with the plugins' and those written over the API (DR-31, DR-33; see [Registration](registration.md)); a reasoner it registers has no other source and cannot be replaced over the API | blank (no file) |
| `DECISIONLEDGER_IDENTITY_RESOLVER` | Framework | The one active identity resolver: an entry-point name in `decisionledger.identity_resolvers` (DR-32); a name that is not installed refuses startup | blank (the passthrough; `passthrough` is the same) |
| `DECISIONLEDGER_API_KEYS` | Framework | Reasoner API keys as comma-separated `reasoner_id:key` pairs | blank (no reasoner keys; with no key at all, every route but `/health` answers 401) |
| `DECISIONLEDGER_API_KEYS_FILE` | Framework | Path to a YAML file mapping `reasoner_id` to a key or a list of keys, read in addition | blank (no file) |
| `DECISIONLEDGER_OPERATOR_KEY` | Framework | The operator key: reads every reasoner's decisions, resolves held ones, and registers reasoners over the API (DR-33) | blank (operator access disabled) |
| `DECISIONLEDGER_EVIDENCE_ESCROW` | Framework | `on` or `off` (any case): whether gates' evidence is escrowed in `evidence_artifacts` (DR-34). Read by the bootstrap only; any other value refuses startup. Off, nothing is written and references name no artifact; rows written earlier stay readable | `on` |
| `DECISIONLEDGER_EVAL_REASONER` | Eval harness | Reasoner to evaluate when `--reasoner-id` is not passed | none |

### Gates

| Variable | Read by | Description | Default |
|----------|---------|-------------|---------|
| `RULES_GATE_RULESETS_DIR` | Rules gate | Directory of rule-set YAML files the gate registers at startup | blank (no rule sets) |
| `POLICY_RAG_GATE_ELASTICSEARCH_URL` | RAG policy gate | The gate's Elasticsearch | `http://localhost:9200` |
| `POLICY_RAG_GATE_ASSETS_ROOT` | RAG policy gate, corpus loader | Asset root of reasoners registered without a plugin: `<root>/<reasoner_id>/prompts/*.yaml` and `corpus/**/*.md` (DR-33; see [Registration](registration.md)) | blank (plugin reasoners only) |
| `POLICY_RAG_GATE_ELASTICSEARCH_PORT` | Compose (`gates/policy_rag/compose.yaml`) | Elasticsearch host port | `9200` |
| `OPENAI_API_KEY` | RAG policy gate (and the OpenAI SDK) | The gate's LLM client and its eval's faithfulness scorer | none (required to start any ledger that composes the RAG policy gate, including one run in process through `LocalClient`, even if no decision reaches the model) |
| `ANTHROPIC_API_KEY` | RAG policy gate (and the Anthropic SDK) | The gate eval's LLM judges | none (required for `make eval-gate-policy_rag`) |

The RAG policy gate's Postgres tables live in the framework's database, so the
gate connects with the `POSTGRES_*` settings above.

### ATO reasoner

| Variable | Read by | Description | Default |
|----------|---------|-------------|---------|
| `ATO_REDIS_URL` | ATO reasoner | Redis for feature windows, dedup, and the login-event stream | `redis://localhost:6379` |
| `ATO_REDIS_PORT` | Compose (`reasoners/account_takeover/compose.yaml`) | Redis host port | `6379` |
| `ATO_SCORER_MODEL_PATH` | ATO reasoner | XGBoost model artifact | absolute path of `ato_reasoner/scorer/models/ato-v1.ubj` inside the installed package |
| `ATO_TEMPLATE_ID` | ATO reasoner | The RAG policy gate template ATO names in its gate context: `ato-v4` (tiered evidence with the conflict/gap protocol), `ato-v3` (the behavior before DR-34), or `ato-v2`; any other value refuses startup (`ato-v1` cannot be rendered again) | `ato-v4` |
| `ATO_LEDGER_URL` | ATO reasoner | The ledger ATO's service submits to over HTTP | blank (the ledger runs in ATO's process, through `LocalClient`) |
| `ATO_LEDGER_KEY` | ATO reasoner | ATO's API key on that ledger (one of its `DECISIONLEDGER_API_KEYS`) | blank |

### Reference-pattern mocks

| Variable | Read by | Description | Default |
|----------|---------|-------------|---------|
| `MOCK_STREAM_LEDGER_URL` | Stream mock | The ledger the stream mock's own service (`make serve-mock_stream`) submits to over HTTP | blank (the service refuses to start; it never falls back to a ledger in its own process) |
| `MOCK_STREAM_LEDGER_KEY` | Stream mock | The stream mock's API key on that ledger (one of its `DECISIONLEDGER_API_KEYS`) | blank |
| `MOCK_STREAM_WINDOW_SECONDS` | Stream mock | How far back, in event time, a transfer's per-account window reaches | `600` |
| `MOCK_STREAM_ALLOWED_LATENESS_SECONDS` | Stream mock | How far behind the stream's watermark an event may arrive and still be decided | `300` |
| `MOCK_STREAM_HISTORY` | Stream mock | How many recent decisions the service lists (`GET /api/v1/stream/decisions`, 1 to 100,000); the ledger keeps every bundle | `1000` |
| `MOCK_BATCH_LEDGER_URL` | Batch mock | The ledger the batch mock's `run` (`make run-mock_batch`) submits to over HTTP | blank (`run` refuses to start) |
| `MOCK_BATCH_LEDGER_KEY` | Batch mock | The batch mock's API key on that ledger (one of its `DECISIONLEDGER_API_KEYS`) | blank |
| `MOCK_TEXT_LEDGER_URL` | Text mock | The ledger the text mock's `decide` submits to over HTTP | blank (`decide` refuses to start) |
| `MOCK_TEXT_LEDGER_KEY` | Text mock | The text mock's API key on that ledger (one of its `DECISIONLEDGER_API_KEYS`) | blank |
| `MOCK_DAILY_LEDGER_URL` | Daily mock | The ledger the daily mock's `run` (`make run-mock_daily`) submits to over HTTP | blank (`run` refuses to start) |
| `MOCK_DAILY_LEDGER_KEY` | Daily mock | The daily mock's API key on that ledger (one of its `DECISIONLEDGER_API_KEYS`) | blank |

### Clients

These are read by programs that call a running ledger over HTTP, not by the
ledger.

| Variable | Read by | Description | Default |
|----------|---------|-------------|---------|
| `DECISIONLEDGER_URL` | MCP server, conformance suite | The ledger's base URL | MCP: `http://localhost:8000`; conformance: none (every test skips without it) |
| `DECISIONLEDGER_KEY` | MCP server, conformance suite | A reasoner's API key (conformance: the example reasoner's) | none |
| `DECISIONLEDGER_OPERATOR_KEY` | MCP server, conformance suite | The operator key (MCP: sent on `resolve_hold` only) | none |
| `DECISIONLEDGER_OTHER_KEY` | Conformance suite | Another reasoner's key, for the 403 and tenant-isolation checks | none (those checks skip) |

Retrieval and enforcement tuning values (the reranker timeout, the
low-confidence cutoff) and budgets (the gate budget, the identity budget) are
code constants, not environment variables. A reasoner's bypass thresholds
(ATO's `allow-band` and `block-band`) live in its registration. See
[pipeline.md](pipeline.md) and [decisions.md](decisions.md).

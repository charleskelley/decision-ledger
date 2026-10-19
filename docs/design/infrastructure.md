# Infrastructure

The local development stack, the database schema, the stores that gates and
reasoners own, the production deployment model, and the environment
variables.

The framework's only infrastructure is PostgreSQL. The ledger keeps every
Decision Bundle, replay log, resolution attempt, registration snapshot, and
the identity journal in one schema, `decisionledger`, and needs nothing else
to run. A gate or a reasoner that needs more (a search index, a feature store,
a queue) brings it in its own compose file and configures it under its own
prefix: the framework never connects to it.

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
| `elasticsearch` | RAG policy gate (`gates/policy_rag/compose.yaml`) | `docker.elastic.co/elasticsearch/elasticsearch:8.13.4` | 9200 (`POLICY_RAG_GATE_ELASTICSEARCH_PORT`) | The gate's BM25 index over every reasoner's policy corpus |
| `redis` | ATO reasoner (`reasoners/account_takeover/compose.yaml`) | `redis:7-alpine` | 6379 (`ATO_REDIS_PORT`) | ATO's login-event stream, dedup store, and feature windows |

**Why the Postgres image is `pgvector/pgvector:pg16`.** It is plain Postgres 16
plus the `vector` extension, available for a gate's DDL to create. The
framework's own DDL creates no extension. The RAG policy gate keeps its dense
retrieval chunks on the same server, in its own schema, and its DDL creates
the extension there (see [Gate-owned storage](#gate-owned-storage)).

Nothing in the framework (`core/`, `app/`, `eval/`) uses Redis or
Elasticsearch. ATO opens its own Redis client from `ATO_REDIS_URL`
(`ato_reasoner/plugin.py`); the RAG policy gate opens its own Elasticsearch
client from `POLICY_RAG_GATE_ELASTICSEARCH_URL` when it is built.

The services that run Python are not containerized yet. They run directly with
uv during development:

| Process | Command | Port |
|---------|---------|------|
| The ledger API | `make serve` (`uv run uvicorn --factory app.main:create_app`) | 8000 |
| ATO's own service | `make serve-account_takeover` (`ato_reasoner.app:create_app`) | 8001 |
| The MCP server | `uv run decisionledger-mcp` (stdio) | none |

ATO's service submits to the ledger in its own process through `LocalClient`
(writing to the same Postgres) unless `ATO_LEDGER_URL` points it at a ledger
over HTTP. The [local runbook](../development/runbook-local.md) walks through
a first run.

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
that have one, and the `decision_id` join on the rest (DR-24). The canonical
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
| `entity_events` | The identity journal, append-only (DR-32) | `event_id` (PK), `event_sequence`, `event_type`, `subject_entity_id`, `object_entity_id`, `relationship_type`, `effective_at`, `recorded_at`, `provenance` (JSONB) |
| `entity_membership` | The membership projection, rebuildable (DR-32) | `subject_entity_id`, `related_entity_id`, `relationship_type`, `valid_from`, `valid_to` |

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
The rules gate (`gates/rules`) stores nothing.

#### PostgreSQL schema: `policy_rag`

The gate's DDL ships in its package (`policy_rag_gate/sql/01_init.sql` and
`02_tables.sql`). `01_init.sql` creates the `vector` extension (the connecting
role must be allowed to create it) and the `policy_rag` schema; `02_tables.sql`
creates `policy_rag.policy_chunks`. `policy_rag_gate.schema.apply_schema`
applies both, idempotently, every time the gate is built and before every
corpus load; `make prepare-gate-policy_rag` (or `make prepare-gates`, every
gate) applies them on their own.

```sql
create table if not exists policy_rag.policy_chunks (
    chunk_id uuid primary key default gen_random_uuid(),
    reasoner_id text not null,             -- tenant filter applied at retrieval
    policy_id text not null,
    policy_version text not null,
    title text not null default '',        -- document title, for citations
    jurisdiction text not null,
    risk_tier text,                        -- null means applies to all tiers
    section_path text not null,
    chunk_text text not null,
    embedding vector(384) not null,        -- all-MiniLM-L6-v2 dimensions
    created_at timestamptz not null default now()
);

create index if not exists idx_policy_chunks_hnsw
on policy_rag.policy_chunks
using hnsw (embedding vector_cosine_ops);
```

The `vector(384)` dimension matches the `all-MiniLM-L6-v2` sentence-transformer
model. If the embedding model changes, update this column, the plugin, and the
corpus loader together. `title` lets the retriever build citations from the
row alone, without reading any reasoner's corpus directory.

#### Elasticsearch index: `policy-chunks`

One index, shared by every reasoner that uses the gate. The mapping ships in
the gate's package at `policy_rag_gate/es/policy-chunks.json`; the corpus
loader creates the index on first load if it does not exist.

`reasoner_id` is mapped as `keyword`, and the retriever applies it as a `term`
filter for tenant isolation. An index created before that mapping held
`reasoner_id` as analyzed `text`, where a `term` filter on a hyphenated id
matches nothing. The loader detects that and recreates the index, logging that
it did. Recreating drops every reasoner's BM25 documents, so index every
reasoner afterward (`make build-policy-index`).

The `text` field uses a dual-analyzer multi-field strategy (DR-13):

- `text`: English analyzer with stemming, for natural-language queries.
- `text.keyword_search`: standard analyzer, for exact identifiers (section
  numbers, control ids, policy ids).

The retriever queries both fields with `multi_match`. Retrieval filters:
`reasoner_id` and `jurisdictions` filter both stores; `risk_tier` filters
pgvector only.

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

Each reasoner that binds the RAG policy gate ships its own policy corpus (its
plugin's `corpus_dir`), and the gate's corpus loader indexes it under that
reasoner's `reasoner_id`. A corpus must be loaded before the gate can retrieve
for that reasoner; load it once at setup, and again whenever the corpus
changes.

```bash
# Every installed reasoner's corpus (aggregate over reasoners/*/reasoner.mk)
make build-policy-index

# One reasoner's corpus (the target the gate's gate.mk defines)
make build-policy-index-ato-reasoner

# One reasoner: index its corpus, and (for ATO) train its model if missing
make prepare-account_takeover

# The loader directly (safe to re-run: deletes per policy_id before inserting)
uv run python -m policy_rag_gate.corpus_loader --reasoner-id ato-reasoner

# Full wipe and reload of this reasoner's chunks
uv run python -m policy_rag_gate.corpus_loader --reasoner-id ato-reasoner --wipe

# Explicit corpus root instead of the plugin's corpus_dir
uv run python -m policy_rag_gate.corpus_loader --reasoner-id ato-reasoner --corpus-dir path/to/corpus
```

The loader:

1. Resolves the corpus directory: `--corpus-dir` if given, otherwise the
   `corpus_dir` of the installed reasoner plugin whose `reasoner_id` matches
   `--reasoner-id` (for ATO, `ato_reasoner/corpus/`), and reads every
   `**/*.md` file under it.
2. Parses YAML frontmatter into `PolicyDocument`.
3. Chunks each document at `##` boundaries, with a `###` secondary split for
   sections over 300 words (rules in [the policy corpus page](gates/policy/policy-corpus.md)).
4. Embeds the chunks with `all-MiniLM-L6-v2` (384 dimensions) through
   sentence-transformers.
5. Applies the gate's DDL, then writes embeddings and metadata (including
   `reasoner_id` and `title`) to `policy_rag.policy_chunks`, reading the
   Postgres DSN from `FrameworkSettings`.
6. Creates the `policy-chunks` index if missing (recreating it if its
   `reasoner_id` mapping is stale), then writes text and metadata (no
   embedding) to the Elasticsearch at `POLICY_RAG_GATE_ELASTICSEARCH_URL`.

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
| Ledger API (FastAPI) | Framework | ECS Fargate (or EKS) | Stateless; horizontally scalable |
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

The ledger API is stateless: every decision's state is in Postgres, and the
gates it composes run inside its process. It scales horizontally without
coordination.

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
`ato_reasoner/settings.py`). The LLM providers' keys keep their providers'
names. Port variables marked "Compose" only change a host port mapping in a
compose file.

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
| `DECISIONLEDGER_REASONERS` | Framework | Comma-separated allowlist of `reasoner_id`s to compose from the installed plugins | blank (every installed plugin) |
| `DECISIONLEDGER_MANDATORY_GATES` | Framework | Comma-separated `gate_id`s run on every decision of every reasoner (DR-29); each must be composed, or startup fails | blank (none) |
| `DECISIONLEDGER_REGISTRATIONS` | Framework | Path to a YAML file of reasoner registrations, composed with the plugins' (DR-31) | blank (no file) |
| `DECISIONLEDGER_IDENTITY_RESOLVER` | Framework | The one active identity resolver: an entry-point name in `decisionledger.identity_resolvers` (DR-32); a name that is not installed refuses startup | blank (the passthrough; `passthrough` is the same) |
| `DECISIONLEDGER_API_KEYS` | Framework | Reasoner API keys as comma-separated `reasoner_id:key` pairs | blank (no reasoner keys; with no key at all, every route but `/health` answers 401) |
| `DECISIONLEDGER_API_KEYS_FILE` | Framework | Path to a YAML file mapping `reasoner_id` to a key or a list of keys, read in addition | blank (no file) |
| `DECISIONLEDGER_OPERATOR_KEY` | Framework | The operator key: reads every reasoner's decisions and resolves held ones | blank (operator access disabled) |
| `DECISIONLEDGER_EVAL_REASONER` | Eval harness | Reasoner to evaluate when `--reasoner-id` is not passed | none |

### Gates

| Variable | Read by | Description | Default |
|----------|---------|-------------|---------|
| `RULES_GATE_RULESETS_DIR` | Rules gate | Directory of rule-set YAML files the gate registers at startup | blank (no rule sets) |
| `POLICY_RAG_GATE_ELASTICSEARCH_URL` | RAG policy gate | The gate's Elasticsearch | `http://localhost:9200` |
| `POLICY_RAG_GATE_ELASTICSEARCH_PORT` | Compose (`gates/policy_rag/compose.yaml`) | Elasticsearch host port | `9200` |
| `OPENAI_API_KEY` | RAG policy gate (and the OpenAI SDK) | The gate's LLM client and its eval's faithfulness scorer | none (required for gated decisions that reach the RAG gate) |
| `ANTHROPIC_API_KEY` | RAG policy gate (and the Anthropic SDK) | The gate eval's LLM judges | none (required for `make eval-gate-policy_rag`) |

The RAG policy gate's Postgres tables live in the framework's database, so the
gate connects with the `POSTGRES_*` settings above.

### ATO reasoner

| Variable | Read by | Description | Default |
|----------|---------|-------------|---------|
| `ATO_REDIS_URL` | ATO reasoner | Redis for feature windows, dedup, and the login-event stream | `redis://localhost:6379` |
| `ATO_REDIS_PORT` | Compose (`reasoners/account_takeover/compose.yaml`) | Redis host port | `6379` |
| `ATO_SCORER_MODEL_PATH` | ATO reasoner | XGBoost model artifact | absolute path of `ato_reasoner/scorer/models/ato-v1.ubj` inside the installed package |
| `ATO_CORPUS_VERSION` | ATO reasoner | Corpus version ATO sends in its RAG policy gate context (`corpus_version`), so every ATO bundle records it | `ato-v1` |
| `ATO_LEDGER_URL` | ATO reasoner | The ledger ATO's service submits to over HTTP | blank (the ledger runs in ATO's process, through `LocalClient`) |
| `ATO_LEDGER_KEY` | ATO reasoner | ATO's API key on that ledger (one of its `DECISIONLEDGER_API_KEYS`) | blank |

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

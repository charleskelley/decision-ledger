# Infrastructure

Local development stack, production deployment model, and AWS Terraform modules.

---

## Local Development Stack

All infrastructure runs locally via Docker Compose V2. A single `docker compose up -d` brings up the full stack — no cloud account required for development or testing.

The root `docker-compose.yml` defines only the framework's shared services, PostgreSQL and Elasticsearch. Infrastructure that one reasoner needs for itself lives in that reasoner's own compose file and is pulled into the same stack with a top-level `include:` (Compose 2.20 or later):

```yaml
include:
  - reasoners/account_takeover/compose.yaml   # Redis for the ATO reasoner
```

```bash
docker compose up -d        # Start all services in background
docker compose down         # Stop services (volumes preserved)
docker compose down -v      # Full reset — removes all volumes and data
docker compose logs -f      # Tail all logs
```

### Services

Three services. The FastAPI service is not containerized yet — it runs directly via `uv run uvicorn --factory app.main:create_app` during development.

| Service | Owner (compose file) | Image | Port | Purpose |
|---------|----------------------|-------|------|---------|
| `postgres` | Framework (`docker-compose.yml`) | `pgvector/pgvector:pg16` | 5432 | Policy vector store (pgvector HNSW) + audit store (Decision Bundles), shared by every reasoner |
| `elasticsearch` | Framework (`docker-compose.yml`) | `elasticsearch:8.13.4` | 9200 | BM25 sparse retrieval index, shared by every reasoner |
| `redis` | ATO reasoner (`reasoners/account_takeover/compose.yaml`) | `redis:7-alpine` | 6379 (`ATO_REDIS_PORT`) | ATO login-event stream, dedup store, and feature windows |

Nothing in the framework (`core/`, `app/`, `eval/`) uses Redis. The ATO plugin opens its own client from `ATO_REDIS_URL` in `build_services`.

### Resource Expectations (M1 Max, 64GB RAM)

The full stack — Redis, PostgreSQL + pgvector, and Elasticsearch — is validated on an M1 Max 64GB. Elasticsearch is the most resource-intensive service; the `docker-compose.yml` sets `ES_JAVA_OPTS="-Xms512m -Xmx512m"` for development. Security is disabled for the Elasticsearch local service (`xpack.security.enabled=false`) — never expose to a public network.

---

## Database Schema

### PostgreSQL — Schema: `decisionledger`

All tables live under the framework-owned `decisionledger` schema and are shared by every reasoner; tenant separation is the `reasoner_id` column (DR-24). The canonical DDL ships inside the framework package: `app/audit/sql/01_init.sql` creates the `vector` extension and the schema, and `app/audit/sql/02_tables.sql` creates every table and index. The same files are applied three ways, and all three are idempotent (`create ... if not exists`):

- Compose mounts them into `/docker-entrypoint-initdb.d/`, so a fresh Postgres volume initializes on first start.
- `app/audit/schema.py:apply_canonical_schema` runs them on every API start (from `app/bootstrap.py`) and before every corpus load.
- The CI integration and eval workflows apply them with `psql` against the service container.

The excerpts below omit indexes; `02_tables.sql` is authoritative.

#### `decision_bundles` — Audit store

```sql
create table if not exists decisionledger.decision_bundles (
    decision_id     uuid        primary key,
    reasoner_id     text        not null,   -- tenant column: the reasoner that produced the decision
    entity_id       uuid        not null,   -- UUID5 of the reasoner's business key (framework identity)
    created_at      timestamptz not null,
    decision_action text        not null,   -- ALLOW | CHALLENGE | HOLD | BLOCK (immutable; pipeline verdict)
    bundle          jsonb       not null    -- full DecisionBundle serialized
);
```

`entity_id` is the framework-level identity derived from the reasoner's business key via UUID5. Reasoner business keys (ATO's `account_id`) live inside the JSONB `bundle`, not as columns. Lookup and replay read `reasoner_id` to pick the reasoner's raw-event deserializer; a row whose reasoner is not composed in the running deployment answers 409 `REASONER_NOT_COMPOSED`.

#### `policy_chunks` — pgvector HNSW index

```sql
create table if not exists decisionledger.policy_chunks (
    chunk_id       uuid        primary key default gen_random_uuid(),
    reasoner_id    text        not null,             -- tenant filter applied at retrieval
    policy_id      text        not null,
    policy_version text        not null,
    title          text        not null default '',  -- document title, denormalized for citations
    jurisdiction   text        not null,
    risk_tier      text,                             -- NULL means applies to all tiers
    section_path   text        not null,
    chunk_text     text        not null,
    embedding      vector(384) not null,             -- all-MiniLM-L6-v2 dimensions
    created_at     timestamptz not null default now()
);

-- Idempotent migration for volumes created before title existed.
alter table decisionledger.policy_chunks
add column if not exists title text not null default '';

create index if not exists idx_policy_chunks_hnsw
    on decisionledger.policy_chunks
    using hnsw (embedding vector_cosine_ops);
```

The `vector(384)` dimension matches the `all-MiniLM-L6-v2` sentence-transformer model. If the embedding model changes, update both this column definition and the corpus loader.

`title` lets the retriever build citations from the row alone, without reading any reasoner's corpus directory. Rows loaded before the column existed hold `''` until the corpus is reloaded with `--wipe`.

#### `replay_logs` — Enforcement replay audit trail

```sql
create table if not exists decisionledger.replay_logs (
    replay_id    uuid        primary key default gen_random_uuid(),
    decision_id  uuid        not null
                             references decisionledger.decision_bundles (decision_id),
    replayed_at  timestamptz not null default now(),
    is_matched   boolean     not null,    -- true = same action as original
    diff         jsonb                    -- populated only when is_matched = false
);
```

`decision_resolution_attempts` (the append-only resolution log, DR-18/DR-21) is also defined in `02_tables.sql`. Neither it nor `replay_logs` carries `reasoner_id`; both reach it through the `decision_id` join.

---

## Redis Key Space (ATO reasoner)

Redis belongs to the ATO reasoner, and every key is prefixed `ato:`. The table is a reference for the reasoner's own code (`ato_reasoner/ingestion/`, `ato_reasoner/feature_service.py`, `ato_reasoner/generator/`); the framework never reads these keys.

| Key Pattern | Type | TTL | Purpose |
|-------------|------|-----|---------|
| `ato:login-events` | Stream | — | Login-event queue (produced by the scenario generator) |
| `ato:login-events:dlq` | Stream | — | Dead-letter queue (schema errors, lateness, duplicates) |
| `ato:dedup:<sha256>` | String | 24 h | Idempotency store — SHA-256 of `event_id`, `SET NX EX` |
| `ato:velocity:{account_id}` | Sorted Set | 48 h | Sliding-window event timestamps |
| `ato:ips:{account_id}` | Set | 90 d | Seen IP addresses |
| `ato:devices:{account_id}` | Set | 90 d | Seen device fingerprints |
| `ato:countries:{account_id}` | Set | 90 d | Seen country codes |
| `ato:ua:{account_id}` | Set | 90 d | Seen user-agent strings |
| `ato:last_geo:{account_id}` | Hash | 90 d | Last lat/lon + timestamp, for impossible-travel detection |
| `ato:total:{account_id}` | String | — | Lifetime event count |

---

## Elasticsearch Index: `policy-chunks`

One index, shared by every reasoner. The mapping ships inside the framework package at `app/retrieval/es/policy-chunks.json`. The corpus loader creates the index on first load if it does not exist.

`reasoner_id` is mapped as `keyword`, and the retriever applies it as a `term` filter for tenant isolation. An index created before that mapping existed holds `reasoner_id` as analyzed `text`, where a `term` filter on a hyphenated id such as `ato-reasoner` matches nothing. The loader detects that and recreates the index, logging that it did. Recreating drops every reasoner's BM25 documents, so run the loader once per reasoner afterward (`make build-policy-index` does all of them).

Key design decision (DR-13): the `text` field uses a dual-analyzer multi-field strategy:

- `text` — English analyzer with stemming, for natural language queries
- `text.keyword_search` — Standard analyzer, for exact regulatory identifiers (`AAL2`, `NIST 800-63B §5.2.3`, `INT-RISK-V2`)

The retriever queries both fields via `multi_match` to handle both term types in a single query.

---

## Policy Corpus Build

Each reasoner ships its own policy corpus (the plugin's `corpus_dir`), and the framework's corpus loader indexes it under that reasoner's `reasoner_id`. A corpus must be loaded into pgvector and Elasticsearch before the retrieval layer can serve that reasoner's queries. This is a one-time operation at setup, and re-run whenever the corpus changes.

```bash
# Every installed reasoner (aggregate over reasoners/*/reasoner.mk)
make build-policy-index

# One reasoner — index its corpus, and train its model if missing
make prepare-account_takeover

# The loader directly (safe to re-run — deletes per policy_id before inserting)
uv run python -m app.retrieval.corpus_loader --reasoner-id ato-reasoner

# Full wipe and reload of this reasoner's chunks
uv run python -m app.retrieval.corpus_loader --reasoner-id ato-reasoner --wipe

# Explicit corpus root instead of the plugin's corpus_dir
uv run python -m app.retrieval.corpus_loader --reasoner-id ato-reasoner --corpus-dir path/to/corpus
```

This command:

1. Applies the canonical DDL, then resolves the corpus directory: `--corpus-dir` if given, otherwise the `corpus_dir` of the installed plugin whose `reasoner_id` matches `--reasoner-id` (for ATO, `ato_reasoner/corpus/`). It reads every `**/*.md` file under it (ATO: guidance/, regulations/, standards/, internal/)
2. Parses YAML frontmatter into `PolicyDocument`
3. Chunks each document at `##` boundaries; `###` secondary split for sections > 300 words
4. Embeds chunks using `all-MiniLM-L6-v2` (384 dimensions) via sentence-transformers
5. Writes embeddings + metadata (including `reasoner_id` and `title`) to `decisionledger.policy_chunks` in PostgreSQL
6. Creates the `policy-chunks` Elasticsearch index if missing (recreating it if its `reasoner_id` mapping is stale), then writes text + metadata (no embedding)

Verify the load:

```bash
# PostgreSQL — chunk count per policy document
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c "SELECT policy_id, COUNT(*) AS chunks FROM decisionledger.policy_chunks GROUP BY policy_id ORDER BY chunks DESC;"

# Elasticsearch — total chunk count
curl -s localhost:9200/policy-chunks/_count | python3 -m json.tool
```

---

## Production Deployment Model (AWS)

<!-- Terraform module details to be filled in during Week 7. -->
<!-- This section documents the target architecture. Implementation is a Week 7 stretch goal. -->

The production deployment targets AWS. The local Docker Compose services map to AWS managed services:

| Local | AWS Equivalent | Notes |
|-------|---------------|-------|
| Redis Streams (ATO reasoner) | ElastiCache for Redis | Cluster mode for partitioned streams; provisioned per reasoner that needs it |
| PostgreSQL + pgvector | RDS PostgreSQL with pgvector extension | Or Aurora PostgreSQL |
| Elasticsearch | Amazon OpenSearch Service | Same BM25 functionality |
| FastAPI service | ECS Fargate (or EKS) | Stateless — horizontally scalable |

### Terraform Modules

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

The API service and policy gate service are stateless — horizontally scalable without coordination.

The feature service is **not** trivially scalable: it maintains sliding window state in Redis sorted sets keyed by `account_id`. A naive scale-out produces correct results (Redis is the source of truth), but requires care during graceful shutdown to avoid corrupting in-flight window state.

At production scale, Redis Streams is replaced with Kafka or AWS Kinesis (see DR-1 in [decisions.md](./decisions.md)). Consumer group partitioning by `account_id` ensures all events for a given account route to the same feature service instance, keeping window state local to one instance.

---

## Environment Configuration

All runtime configuration via environment variables. No secrets in code or committed config files. Copy `.env.example` to `.env` for local overrides.

Two namespaces. Unprefixed variables configure the framework's shared infrastructure and are read by `FrameworkSettings` (`app/settings.py`); each reasoner reads its own variables under its own prefix (`ATO_` for Account Takeover, via `AtoSettings` in `ato_reasoner/settings.py`). Port variables marked "Compose" only change the host port mapping in the compose files.

| Variable | Read by | Description | Default |
|----------|---------|-------------|---------|
| `POSTGRES_HOST` | Framework | PostgreSQL host | `localhost` |
| `POSTGRES_PORT` | Framework, Compose | PostgreSQL port | `5432` |
| `POSTGRES_USER` | Framework, Compose | PostgreSQL user | `decisionledger` |
| `POSTGRES_PASSWORD` | Framework, Compose | PostgreSQL password | `decisionledger` |
| `POSTGRES_DB` | Framework, Compose | PostgreSQL database | `decisionledger` |
| `ELASTICSEARCH_URL` | Framework | Elasticsearch endpoint | `http://localhost:9200` |
| `ELASTICSEARCH_PORT` | Compose | Elasticsearch host port | `9200` |
| `OPENAI_API_KEY` | Framework | OpenAI key (policy gate) | — (required for gate-routed decisions) |
| `ANTHROPIC_API_KEY` | Framework | Anthropic key (eval judges) | — (required for `make eval`) |
| `LOG_JSON` | Framework | JSON log rendering | `true` |
| `LOG_LEVEL` | Framework | Log level | `INFO` |
| `DECISIONLEDGER_REASONERS` | Framework | Comma-separated allowlist of `reasoner_id`s to compose | blank (every installed plugin) |
| `DECISIONLEDGER_EVAL_REASONER` | Eval harness | Reasoner to evaluate when `--reasoner-id` is not passed | — |
| `ATO_REDIS_URL` | ATO reasoner | Redis for feature windows, dedup, and the login-event stream | `redis://localhost:6379` |
| `ATO_REDIS_PORT` | Compose (`reasoners/account_takeover/compose.yaml`) | Redis host port | `6379` |
| `ATO_SCORER_MODEL_PATH` | ATO reasoner | XGBoost model artifact | absolute path of `ato_reasoner/scorer/models/ato-v1.ubj` inside the installed package |
| `ATO_CORPUS_VERSION` | ATO reasoner | Corpus version recorded in every Decision Bundle | `ato-v1` |

Retrieval and enforcement tuning values (reranker timeout, fast-path thresholds, low-confidence cutoff) are code constants, not environment variables; see [pipeline.md](pipeline.md) and DR-25 in [decisions.md](decisions.md).

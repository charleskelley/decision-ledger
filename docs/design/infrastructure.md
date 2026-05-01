# Infrastructure

Local development stack, production deployment model, and AWS Terraform modules.

---

## Local Development Stack

All infrastructure runs locally via Docker Compose V2. A single `docker compose up -d` brings up the full stack — no cloud account required for development or testing.

```bash
docker compose up -d        # Start all services in background
docker compose down         # Stop services (volumes preserved)
docker compose down -v      # Full reset — removes all volumes and data
docker compose logs -f      # Tail all logs
```

### Services

Three services. The FastAPI service is not containerized yet — it runs directly via `uv run` during development.

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| `redis` | `redis:7-alpine` | 6379 | Event queue (Redis Streams) + dedup store |
| `postgres` | `pgvector/pgvector:pg16` | 5432 | Policy vector store (pgvector HNSW) + audit store (Decision Bundles) |
| `elasticsearch` | `elasticsearch:8.13.4` | 9200 | BM25 sparse retrieval index |

### Resource Expectations (M1 Max, 64GB RAM)

The full stack — Redis, PostgreSQL + pgvector, and Elasticsearch — is validated on an M1 Max 64GB. Elasticsearch is the most resource-intensive service; the `docker-compose.yml` sets `ES_JAVA_OPTS="-Xms512m -Xmx512m"` for development. Security is disabled for the Elasticsearch local service (`xpack.security.enabled=false`) — never expose to a public network.

---

## Database Schema

### PostgreSQL — Schema: `account_takeover`

All tables live under the `account_takeover` schema, created by `infra/postgres/01_init.sql` on first container start. Tables are defined in `infra/postgres/02_tables.sql`.

#### `decision_bundles` — Audit store

```sql
create table if not exists account_takeover.decision_bundles (
    decision_id   uuid        primary key,
    entity_id     uuid        not null,    -- UUID5 from account_id (framework identity)
    account_id    text        not null,    -- ATO domain business key
    created_at    timestamptz not null,
    decision_action text      not null,    -- ALLOW | CHALLENGE | HOLD | BLOCK (immutable; pipeline verdict)
    bundle        jsonb       not null     -- Full DecisionBundle serialized
);
```

`entity_id` is the framework-level identity derived from `account_id` via UUID5. Use `entity_id` for cross-domain or framework queries; use `account_id` for operational queries.

#### `policy_chunks` — pgvector HNSW index

```sql
create table if not exists account_takeover.policy_chunks (
    chunk_id      uuid        primary key default gen_random_uuid(),
    policy_id     text        not null,
    version       text        not null,
    jurisdiction  text        not null,
    risk_tier     text,                    -- NULL means applies to all tiers
    section_path  text        not null,
    text          text        not null,
    embedding     vector(384) not null,    -- all-MiniLM-L6-v2 dimensions
    created_at    timestamptz not null default now()
);

create index if not exists idx_policy_chunks_hnsw
    on account_takeover.policy_chunks
    using hnsw (embedding vector_cosine_ops);
```

The `vector(384)` dimension matches the `all-MiniLM-L6-v2` sentence-transformer model. If the embedding model changes, update both this column definition and the corpus loader.

#### `replay_logs` — Enforcement replay audit trail

```sql
create table if not exists account_takeover.replay_logs (
    replay_id    uuid        primary key default gen_random_uuid(),
    decision_id  uuid        not null
                             references account_takeover.decision_bundles (decision_id),
    replayed_at  timestamptz not null default now(),
    matched      boolean     not null,    -- true = same action as original
    diff         jsonb                    -- populated only when matched = false
);
```

---

## Redis Key Space

| Key Pattern | Type | TTL | Purpose |
|-------------|------|-----|---------|
| `ato:login-events` | Stream | — | Primary event queue (produced by generator) |
| `ato:login-events:dlq` | Stream | — | Dead-letter queue (schema errors, lateness, duplicates) |
| `ato:dedup:<sha256>` | String | 24h | Idempotency store — SHA-256 of event_id, SET NX EX |
| `velocity:{account_id}:{window}` | Sorted Set | — | Sliding window event counts (feature layer — not yet built) |
| `history:ip:{account_id}` | Set | — | Known IPs per account (feature layer — not yet built) |
| `history:device:{account_id}` | Set | — | Known device fingerprints (feature layer — not yet built) |
| `history:geo:{account_id}` | Set | — | Known countries per account (feature layer — not yet built) |
| `last_location:{account_id}` | Hash | — | lat/lon + timestamp for impossible travel detection (feature layer — not yet built) |

Feature layer keys are documented for forward reference — `app/features/` is not yet implemented.

---

## Elasticsearch Index: `policy-chunks`

The index mapping is in `infra/elasticsearch/policy-chunks.json`. The corpus loader creates the index on first load if it does not exist.

Key design decision (DR-13): the `text` field uses a dual-analyzer multi-field strategy:

- `text` — English analyzer with stemming, for natural language queries
- `text.keyword_search` — Standard analyzer, for exact regulatory identifiers (`AAL2`, `NIST 800-63B §5.2.3`, `INT-RISK-V2`)

The retriever queries both fields via `multi_match` to handle both term types in a single query.

---

## Policy Corpus Build

The corpus must be loaded into pgvector and Elasticsearch before the retrieval layer can serve queries. This is a one-time operation at setup, and re-run whenever the corpus changes.

```bash
# Load all 32 corpus documents (safe to re-run — deletes per policy_id before inserting)
uv run python -m app.retrieval.corpus_loader

# Full wipe and reload
uv run python -m app.retrieval.corpus_loader --wipe
```

This command:

1. Reads all `corpus/**/*.md` files (guidance/, regulations/, standards/, internal/)
2. Parses YAML frontmatter into `PolicyDocument`
3. Chunks each document at `##` boundaries; `###` secondary split for sections > 300 words
4. Embeds chunks using `all-MiniLM-L6-v2` (384 dimensions) via sentence-transformers
5. Writes embeddings + metadata to `account_takeover.policy_chunks` in PostgreSQL
6. Writes text + metadata (no embedding) to the `policy-chunks` Elasticsearch index

Verify the load:

```bash
# PostgreSQL — chunk count per policy document
docker compose exec postgres psql -U account_takeover -d account_takeover \
  -c "SELECT policy_id, COUNT(*) AS chunks FROM account_takeover.policy_chunks GROUP BY policy_id ORDER BY chunks DESC;"

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
| Redis Streams | ElastiCache for Redis | Cluster mode for partitioned streams |
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

| Variable | Description | Default |
|----------|-------------|---------|
| `REDIS_URL` | Redis connection string | `redis://localhost:6379` |
| `REDIS_PORT` | Redis port (Docker Compose override) | `6379` |
| `POSTGRES_DSN` | PostgreSQL connection string | `postgresql://account_takeover:account_takeover@localhost:5432/account_takeover` |
| `POSTGRES_USER` | PostgreSQL user (Docker Compose) | `account_takeover` |
| `POSTGRES_PASSWORD` | PostgreSQL password (Docker Compose) | `account_takeover` |
| `POSTGRES_DB` | PostgreSQL database (Docker Compose) | `account_takeover` |
| `POSTGRES_PORT` | PostgreSQL port (Docker Compose override) | `5432` |
| `ELASTICSEARCH_URL` | Elasticsearch endpoint | `http://localhost:9200` |
| `ELASTICSEARCH_PORT` | Elasticsearch port (Docker Compose override) | `9200` |
| `OPENAI_API_KEY` | OpenAI API key | — (required) |
| `OPENAI_MODEL` | Model name | `gpt-4o` |
| `SCORER_MODEL_PATH` | Path to serialized XGBoost model | `app/scorer/models/current.json` |
| `ACTIVE_PROMPT_VERSION` | Prompt version tag | `v1` |
| `RERANK_ENABLED` | Enable cross-encoder reranking | `true` |
| `RERANK_TIMEOUT_MS` | Reranker latency budget | `100` |
| `LOW_CONFIDENCE_THRESHOLD` | Scorer fast-path ALLOW cutoff | `0.20` |
| `HIGH_CONFIDENCE_THRESHOLD` | Scorer fast-path BLOCK cutoff | `0.85` |
| `NOVEL_ENTITY_MIN_EVENTS` | Minimum events before full enforcement | `5` |

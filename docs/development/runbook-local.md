# Local Development Runbook

Bring-up, verification, and operational procedures for the local development stack.

This document tracks what can actually be run today. Each section is added as the corresponding pipeline component is built. The current state reflects week 2 completion: infrastructure, corpus loading, event generation, and ingestion are all operational.

---

## Prerequisites

- Docker Desktop running (or Docker Engine on Linux)
- `uv` installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Dependencies installed: `uv sync`
- `.env` file created from `.env.example` (optional — defaults work for local dev)

---

## 1. Infrastructure

### Start all services

```bash
docker compose up -d
```

### Verify health

All three services must be healthy before running any pipeline component.

```bash
# Redis
docker compose exec redis redis-cli ping
# Expected: PONG

# PostgreSQL
docker compose exec postgres pg_isready -U decisionledger -d decisionledger
# Expected: /var/run/postgresql:5432 - accepting connections

# Elasticsearch
curl -s localhost:9200/_cluster/health | python3 -m json.tool | grep '"status"'
# Expected: "status": "green" or "yellow"
```

### Verify PostgreSQL schema

```bash
# pgvector extension installed
docker compose exec postgres psql -U decisionledger -d decisionledger -c '\dx'
# Expected: vector listed in extensions

# decisionledger schema exists
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c '\dn'
# Expected: decisionledger schema listed

# Tables exist
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c '\dt decisionledger.*'
# Expected: decision_bundles, policy_chunks, replay_logs
```

### Stop services

```bash
docker compose down          # Stop; volumes preserved
docker compose down -v       # Full reset — destroys all data
```

---

## 2. Policy Corpus

The corpus must be loaded before the retrieval layer can serve queries. This is a prerequisite for any pipeline run that touches policy retrieval.

### Load

```bash
uv run python -m app.retrieval.corpus_loader --reasoner-id ato-reasoner
```

Expected output: progress lines per document, then a summary. Typical run: 32 documents → ~215 chunks. The first run is slower (model download on first use); subsequent runs use the cached model.

### Verify

```bash
# PostgreSQL — chunk count per policy document
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c "SELECT policy_id, COUNT(*) AS chunks FROM decisionledger.policy_chunks GROUP BY policy_id ORDER BY chunks DESC LIMIT 10;"

# PostgreSQL — total chunk count
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c "SELECT COUNT(*) FROM decisionledger.policy_chunks;"
# Expected: ~215

# Elasticsearch — total chunk count
curl -s localhost:9200/policy-chunks/_count | python3 -m json.tool
# Expected: {"count": ~215, ...}

# Elasticsearch — sample document
curl -s 'localhost:9200/policy-chunks/_search?size=1' | python3 -m json.tool | head -40
```

### Reload (after corpus changes)

```bash
# Safe re-run — deletes per policy_id before inserting, no duplicates
uv run python -m app.retrieval.corpus_loader --reasoner-id ato-reasoner

# Full wipe and reload
uv run python -m app.retrieval.corpus_loader --reasoner-id ato-reasoner --wipe
```

---

## 3. Synthetic Event Generator

### List available scenarios

```bash
uv run python -m generator list
```

Eight scenarios are available:

| Scenario | Expected Action | Key Signals |
|----------|----------------|-------------|
| `baseline_normal` | ALLOW | Stable device, static geo, mostly success |
| `credential_stuffing_burst` | BLOCK | Rotating device, erratic geo, high failure rate |
| `device_fingerprint_anomaly` | CHALLENGE/HOLD | Partially rotating fingerprint |
| `geo_impossible` | BLOCK | Impossible travel between events |
| `post_breach_ato` | HOLD | High success rate post-compromise |
| `high_velocity_legitimate` | CHALLENGE | High velocity but consistent device/geo |
| `novel_entity` | HOLD | No account history (sparse baseline) |
| `adversarial_probe` | BLOCK | User-agent injection payload |

### Dry run — inspect event output

```bash
# Inspect 5 baseline events without publishing
uv run python -m generator run --scenario baseline_normal --count 5 --dry-run
```

Check the output:
- `scenario_tag` — matches the scenario name (stripped by ingestion consumer before pipeline sees it)
- `device_fingerprint` — colon-separated tokens (e.g. `abc123:def456:...`)
- `geo.country` — should be consistent across events for STATIC geo scenarios
- `outcome` — weighted by scenario config

```bash
# Compare credential stuffing — multiple countries, rotating devices, failures
uv run python -m generator run --scenario credential_stuffing_burst --count 10 --dry-run

# Adversarial probe — check user_agent for injection payload
uv run python -m generator run --scenario adversarial_probe --count 3 --dry-run
```

### Publish to Redis

```bash
# Push 20 baseline events to the stream
uv run python -m generator run --scenario baseline_normal --count 20
```

Inspect the stream:

```bash
# Stream length
docker compose exec redis redis-cli XLEN ato:login-events

# Read the most recent entry
docker compose exec redis redis-cli XREVRANGE ato:login-events + - COUNT 1

# Read all entries (small count)
docker compose exec redis redis-cli XRANGE ato:login-events - + COUNT 5
```

The stream key is `ato:login-events`. Each entry has a single `data` field containing the JSON-serialized `LoginEvent`.

---

## 4. Ingestion Consumer

The ingestion consumer reads from the Redis Stream, validates each event, enforces bounded lateness and deduplication, and delivers clean events to the next pipeline stage.

### Run the consumer interactively (Python REPL)

```bash
uv run python
```

```python
import redis
from reasoner.account_takeover.ingestion.consumer import IngestionConsumer

r = redis.Redis(decode_responses=True)
consumer = IngestionConsumer(
    r,
    stream_key="ato:login-events",
    group="ato-reasoner",
    consumer="consumer-0",
    lateness_seconds=3600,
    dedup_ttl_seconds=86400,
)
consumer.ensure_group()

events = []
consumer.consume(events.append, batch_size=20, block_ms=500, max_batches=3)

print(f"Accepted: {len(events)}")
for e in events[:3]:
    print(f"  event_id={e.event_id}  account_id={e.account_id}  scenario_tag={e.scenario_tag}")
# scenario_tag should always be None — stripped before delivery
```

### Verify dead-letter queue

```bash
# DLQ length (should be 0 after clean runs)
docker compose exec redis redis-cli XLEN ato:login-events:dlq

# Read DLQ entries
docker compose exec redis redis-cli XRANGE ato:login-events:dlq - + COUNT 5
```

Each DLQ entry has: `reason` (schema_error / lateness / duplicate), `detail`, `original_id`, and `event_id` (when available).

### Test deduplication manually

Push the same event twice, then consume once — first delivery accepted, second dead-lettered:

```python
import json
import redis
from reasoner.account_takeover.ingestion.consumer import IngestionConsumer
from reasoner.account_takeover.events import AuthMethod, AuthOutcome,
    GeoLocation, LoginEvent
from datetime import datetime, UTC

r = redis.Redis(decode_responses=True)

event = LoginEvent(
    event_id="manual-dedup-test-001",
    timestamp=datetime.now(UTC),
    account_id="acc-manual-001",
    session_id="sess-001",
    ip_address="203.0.113.1",
    geo=GeoLocation(latitude=37.77, longitude=-122.42, country="US",
                    city="San Francisco", asn="AS7922"),
    device_fingerprint="fp-test",
    user_agent="Mozilla/5.0",
    auth_method=AuthMethod.PASSWORD,
    outcome=AuthOutcome.SUCCESS,
)
payload = event.model_dump_json()

r.xadd("ato:test-dedup", {"data": payload})
r.xadd("ato:test-dedup", {"data": payload})

consumer = IngestionConsumer(r, stream_key="ato:test-dedup", group="g1",
                             consumer="c0", dedup_ttl_seconds=60)
consumer.ensure_group()

accepted = []
consumer.consume(accepted.append, batch_size=10, block_ms=100, max_batches=1)

dlq = r.xrange("ato:test-dedup:dlq")
print(f"Accepted: {len(accepted)}")  # 1
print(f"DLQ: {len(dlq)}")  # 1
print(f"DLQ reason: {dlq[0][1]['reason']}")  # duplicate
```

### Test bounded lateness manually

```python
from datetime import timedelta

# Event 2 hours old — outside 1h lateness window
old_event = event.model_copy(update={
    "event_id": "manual-late-test-001",
    "timestamp": datetime.now(UTC) - timedelta(hours=2),
})
r.xadd("ato:test-lateness", {"data": old_event.model_dump_json()})

consumer2 = IngestionConsumer(
    r, stream_key="ato:test-lateness", group="g1", consumer="c0", lateness_seconds=3600
)
consumer2.ensure_group()
accepted2 = []
consumer2.consume(accepted2.append, batch_size=5, block_ms=100, max_batches=1)

dlq2 = r.xrange("ato:test-lateness:dlq")
print(f"Accepted: {len(accepted2)}")    # 0
print(f"DLQ reason: {dlq2[0][1]['reason']}")  # lateness
```

---

## 5. Hybrid Retrieval

Query the policy corpus after loading.

### Interactive retrieval (Python REPL)

```bash
uv run python
```

```python
import psycopg
from elasticsearch import Elasticsearch
from sentence_transformers import SentenceTransformer
from app.retrieval.retriever import PolicyRetriever
from pathlib import Path

pg = psycopg.connect(
    "host=localhost dbname=decisionledger user=decisionledger password=decisionledger"
)
es = Elasticsearch("http://localhost:9200")
model = SentenceTransformer("all-MiniLM-L6-v2")
retriever = PolicyRetriever(pg, es, model, corpus_dir=Path("corpus"))
```

```python
# Dense search — MFA query
results = retriever.dense_search("multi-factor authentication TOTP requirement", k=5)
for s in results:
    print(f"{s.policy_id:30s} | {s.section_path:40s} | {s.relevance_score:.4f}")
```

```python
# Sparse BM25 search
results = retriever.sparse_search("account lockout authentication failure threshold", k=5)
for s in results:
    print(f"{s.policy_id:30s} | {s.section_path}")
```

```python
# Full pipeline — impossible travel scenario
from reasoner.account_takeover.events import AuthMethod, AuthOutcome,
    GeoLocation, LoginEvent
from reasoner.account_takeover.scorer import ScorerOutput
from core.decision.actions import ScorerRouting
from core.decision.scorer import Signal
from datetime import datetime, UTC
from uuid import uuid4

event = LoginEvent(
    event_id="review-001",
    timestamp=datetime.now(UTC),
    account_id="acc-review-001",
    session_id="sess-001",
    ip_address="185.100.87.1",
    geo=GeoLocation(latitude=48.85, longitude=2.35, country="FR", city="Paris",
                    asn="AS3215"),
    device_fingerprint="fp-new-device",
    user_agent="Mozilla/5.0",
    auth_method=AuthMethod.PASSWORD,
    outcome=AuthOutcome.FAILURE,
)
scorer = ScorerOutput(
    entity_id=uuid4(),
    risk_score=0.78,
    top_signals=[
        Signal(feature_name="impossible_travel", shap_value=0.41,
               raw_value=1.0),
        Signal(feature_name="device_novelty", shap_value=0.22, raw_value=1.0),
        Signal(feature_name="velocity_1min", shap_value=0.15, raw_value=8.0),
    ],
    scorer_version="xgb-v1.0.0",
    inference_latency_ms=1.2,
    routing=ScorerRouting.ROUTE_TO_GATE,
)

query = retriever.build_query(event, scorer)
print(f"Query: {query}\n")

snippets, path = retriever.retrieve(query, k=5)
print(f"Retrieval path: {path}")
for s in snippets:
    print(f"  {s.policy_id:30s} | {s.relevance_score:.4f} | {s.section_path}")
```

Expected: `path = "rrf_only"` (cross-encoder reranker is a stub until week 3). Geographic risk and ATO detection policy chunks should surface.

---

## 6. Full Test Suite

```bash
# Unit tests (no Docker required)
make test

# Integration tests (requires docker compose up -d)
make test-integration

# Lint and format
make lint
```

---

## Common Operations

### Reset the event stream

```bash
# Delete and recreate the stream (drops all unprocessed events)
docker compose exec redis redis-cli DEL ato:login-events
docker compose exec redis redis-cli DEL ato:login-events:dlq
```

### Clear the dedup store

Dedup keys expire after 24h automatically. To clear immediately:

```bash
# Clear all dedup keys (use with caution — allows replay of any event)
docker compose exec redis redis-cli --scan --pattern 'ato:dedup:*' | xargs docker compose exec -T redis redis-cli DEL
```

### Inspect a specific Redis stream entry

```bash
# Read entry by ID
docker compose exec redis redis-cli XRANGE ato:login-events <message-id> <message-id>

# Count pending messages (claimed by a consumer group but not yet ACKed)
docker compose exec redis redis-cli XPENDING ato:login-events ato-reasoner - + 10
```

### Reset PostgreSQL tables (keep schema)

```bash
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c "TRUNCATE decisionledger.decision_bundles, decisionledger.replay_logs CASCADE;"

docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c "TRUNCATE decisionledger.policy_chunks CASCADE;"
# Note: after truncating policy_chunks, reload the corpus
```

### Full environment reset

```bash
docker compose down -v     # destroys all volumes
docker compose up -d       # fresh start
uv run python -m app.retrieval.corpus_loader --reasoner-id ato-reasoner   # reload corpus
```

---

## Sections To Be Added

The following sections will be added as each component is built:

- **Section 7 — Feature Computation** (week 4): running `reasoner/account_takeover/feature_service.py`, inspecting velocity and novelty features in Redis
- **Section 8 — XGBoost Scorer** (week 4): training, loading the model, running inference, interpreting fast-path vs. gate routing
- **Section 9 — LLM Policy Gate** (week 5): prompt template management, live gate invocation, reviewing `PolicyGateOutput`
- **Section 10 — Enforcement + Decision Bundle** (week 5–6): full pipeline end-to-end, reading a `DecisionBundle` from Postgres
- **Section 11 — Audit Replay** (week 6): `uv run python -m decision_ledger.audit replay --id <bundle_id>`, `diff` command
- **Section 12 — Evaluation Harness** (week 6): `make eval`, reviewing `EvalReport` output per dimension

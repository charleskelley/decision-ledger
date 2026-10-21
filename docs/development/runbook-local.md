# Local Development Runbook

Bring-up, verification, and operational procedures for the local development stack.

This document tracks what can actually be run today. Section 0 is the end-to-end walk-through; the numbered sections after it drill into each component. Commands that name a reasoner use the reference reasoner, Account Takeover (`reasoners/account_takeover/`, `reasoner_id` `ato-reasoner`).

---

## Prerequisites

- Docker Desktop running (or Docker Engine on Linux) with Compose 2.20 or later (the root compose file uses `include:`)
- `uv` installed (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- `.env` file created from `.env.example` (optional — defaults work for local dev; `OPENAI_API_KEY` is needed for decisions the RAG policy gate runs on, `ANTHROPIC_API_KEY` for that gate's eval, `make eval-gate-policy_rag`)

---

## 0. End-to-End Walk-Through

```bash
# 1. Install the framework and every workspace member: the reasoners
#    (ato-reasoner, example-reasoner), the gates (rules, RAG policy), and the
#    MCP integration, with their entry points
uv sync

# 2. Start Postgres (framework), Elasticsearch (included from
#    gates/policy_rag/compose.yaml), and Redis (included from
#    reasoners/account_takeover/compose.yaml)
docker compose up -d

# 3. Prepare the reasoners: index each policy corpus into the RAG policy gate
#    under its reasoner_id, and train ATO's scorer if the artifact is missing
make prepare-account_takeover prepare-example

# 4. Confirm the plugins are discoverable
uv run python -c "from app.plugins import discover_plugins; print([p.registration.reasoner_id for p in discover_plugins()])"
# Expected: ['ato-reasoner', 'example-reasoner']

# 5. Give the ledger API keys (in .env, or the environment): one per reasoner
#    and the operator's. Every /api/v1 route needs one in X-DecisionLedger-Key.
export DECISIONLEDGER_API_KEYS="example-reasoner:ex-key,ato-reasoner:ato-key"
export DECISIONLEDGER_OPERATOR_KEY=op-key

# 6. Run the ledger API (application factory; serves the framework's routes
#    only — reasoners run as their own services or in process)
uv run uvicorn --factory app.main:create_app --reload     # or: make serve
```

In a second shell:

```bash
# Liveness (no key)
curl -s localhost:8000/health
# Expected: {"status":"ok"}

H='X-DecisionLedger-Key: ex-key'

# Record a widget (the example reasoner's low band: low-score → ALLOW)
uv run python -m example_reasoner sample --band low > /tmp/record.json
curl -s -X POST localhost:8000/api/v1/decisions -H "$H" \
  -H 'content-type: application/json' --data @/tmp/record.json
# Expected: 201, a receipt {"decision_id": ..., "mode": "record",
#   "decision_action": "ALLOW", "bypass_rule_id": "low-score", "gates": [...],
#   "registration_version": ...}, and Location: /api/v1/decisions/<id>

# Ask the gates (mid band → the RAG policy gate; calls OpenAI)
uv run python -m example_reasoner sample --band mid > /tmp/gate.json
curl -s -X POST localhost:8000/api/v1/decisions -H "$H" \
  -H 'content-type: application/json' --data @/tmp/gate.json

# Another reasoner's key cannot write this reasoner's decision
curl -s -X POST localhost:8000/api/v1/decisions -H 'X-DecisionLedger-Key: ato-key' \
  -H 'content-type: application/json' --data @/tmp/record.json
# Expected: 403 application/problem+json {"code": "REASONER_MISMATCH", ...}

# Record 1,000 decisions in one request (NDJSON, records only)
uv run python -m example_reasoner batch --count 1000 > /tmp/widgets.ndjson
curl -s -X POST localhost:8000/api/v1/decisions:batch -H "$H" \
  -H 'content-type: application/x-ndjson' --data-binary @/tmp/widgets.ndjson
# Expected: {"recorded": 1000, "rejected": 0, "results": [...]}

# The bundle, its replay, and the subject's timeline
curl -s localhost:8000/api/v1/decisions/<decision_id> -H "$H" | python3 -m json.tool | head -40
curl -s -X POST localhost:8000/api/v1/decisions/<decision_id>/replay -H "$H"
# Expected: "matched": true, under the bundle's registration_version
curl -s localhost:8000/api/v1/entities/<entity_id>/decisions -H "$H"
# Page with the last item's created_at and decision_id: ?limit=50&before=...&before_id=...

# What the deployment serves: registrations (with versions) and gates
curl -s localhost:8000/api/v1/reasoners -H "$H"
curl -s localhost:8000/api/v1/gates -H "$H"

# Resolve a held decision (operator key; ALLOW or BLOCK)
curl -s -X POST localhost:8000/api/v1/decisions/<held_id>/resolutions \
  -H 'X-DecisionLedger-Key: op-key' -H 'content-type: application/json' \
  --data @conformance/examples/resolution.json
# Errors are RFC 9457 problems with a stable code: 404 DECISION_NOT_FOUND,
# 409 BUNDLE_SCHEMA_UNSUPPORTED (a bundle written before the submission
# contract; lookup still returns it), 409 DECISION_ALREADY_RESOLVED, ...

# ATO as its own service (port 8001), submitting to the ledger over HTTP
ATO_LEDGER_URL=http://localhost:8000 ATO_LEDGER_KEY=ato-key make serve-account_takeover &
uv run python -m ato_reasoner.generator run --scenario baseline_normal --count 1 --dry-run \
  | tail -1 > /tmp/ato-event.json
curl -s -X POST localhost:8001/api/v1/ato/decisions \
  -H 'content-type: application/json' --data @/tmp/ato-event.json
# Expected: {"decision_id": "...", "decision_action": "...", "mode": "record" | "gate",
#            "bypass_rule_id": "allow-band" | "block-band" | null, "gates": [...], ...}
# Without ATO_LEDGER_URL, ATO runs the ledger in its own process (LocalClient).

# The ledger as MCP tools for an agent (stdio)
DECISIONLEDGER_URL=http://localhost:8000 DECISIONLEDGER_KEY=ex-key uv run decisionledger-mcp

# The HTTP contract suite against this instance
DECISIONLEDGER_URL=http://localhost:8000 DECISIONLEDGER_KEY=ex-key \
DECISIONLEDGER_OTHER_KEY=ato-key DECISIONLEDGER_OPERATOR_KEY=op-key make conformance

# Publish 10 baseline_normal events to the ATO Redis stream
make scenario-account_takeover

# Eval for this reasoner (slow; calls OpenAI): the framework's consistency,
# robustness, and shadow dimensions, then the RAG policy gate's retrieval,
# faithfulness, and citation dimensions on ATO's datasets (also calls Anthropic)
make eval-account_takeover
make eval-gate-policy_rag REASONER=ato-reasoner
```

To compose only some installed reasoners, set `DECISIONLEDGER_REASONERS` (comma-separated ids). A reasoner with no plugin installed registers through `DECISIONLEDGER_REGISTRATIONS` (a YAML file; `conformance/registrations.yaml` is an example) or over the API with the operator key (`PUT /api/v1/reasoners/{reasoner_id}`; see [Registration](../design/registration.md)). With none registered, the API still starts and logs `api.no_reasoners`; with no API key configured it logs `api.no_keys` and answers 401 to every `/api/v1` route.

---

## 1. Infrastructure

### Start all services

```bash
docker compose up -d
docker compose config --services
# Expected: elasticsearch, postgres, redis (redis comes from the ATO reasoner's compose.yaml)
```

### Verify health

All three services must be healthy before running any pipeline component. PostgreSQL is the framework's (the RAG policy gate keeps its own `policy_rag` schema in the same database); Elasticsearch belongs to the RAG policy gate; Redis is used only by the ATO reasoner.

```bash
# Redis (ATO reasoner)
docker compose exec redis redis-cli ping
# Expected: PONG

# PostgreSQL
docker compose exec postgres pg_isready -U decisionledger -d decisionledger
# Expected: /var/run/postgresql:5432 - accepting connections

# Elasticsearch (RAG policy gate)
curl -s localhost:9200/_cluster/health | python3 -m json.tool | grep '"status"'
# Expected: "status": "green" or "yellow"
```

### Verify PostgreSQL schema

```bash
# Schemas: decisionledger (framework); policy_rag once the gate's DDL has run
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c '\dn'

# Framework tables
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c '\dt decisionledger.*'
# Expected: decision_bundles, decision_resolution_attempts, entity_events,
#   entity_membership, reasoner_registrations, replay_logs

# RAG policy gate: the vector extension and its chunk table
make prepare-gate-policy_rag   # idempotent; the API and the corpus loader run it too
docker compose exec postgres psql -U decisionledger -d decisionledger -c '\dx'
# Expected: vector listed in extensions
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c '\dt policy_rag.*'
# Expected: policy_chunks
```

The framework's schema comes from `app/audit/sql/01_init.sql` and `02_tables.sql`, mounted into the Postgres init directory on first start; it creates no extension. The RAG policy gate's schema (`policy_rag`, the vector extension, the chunk table) comes from its own DDL in `gates/policy_rag/src/policy_rag_gate/sql/`, applied idempotently when the API builds the gate, before every corpus load, and by `make prepare-gate-policy_rag`. The API also re-applies the framework's DDL on every start, so an older volume picks up new columns without a reset.

**Upgrading a stack created before the RAG gate moved into `gates/policy_rag` (Phase 1b).** Chunks now live in `policy_rag.policy_chunks`, which starts empty: re-index every corpus (`make prepare-reasoners`), or the gate's dense leg finds nothing and decisions run on BM25 evidence alone. The old `decisionledger.policy_chunks` table is no longer read (`docker compose down -v` removes it). `ELASTICSEARCH_URL` and `ELASTICSEARCH_PORT` are now `POLICY_RAG_GATE_ELASTICSEARCH_URL` and `POLICY_RAG_GATE_ELASTICSEARCH_PORT`; the old names are ignored.

### Stop services

```bash
docker compose down          # Stop; volumes preserved
docker compose down -v       # Full reset — destroys all data
```

---

## 2. Policy Corpus

A reasoner's corpus must be loaded before the RAG policy gate can retrieve for it. This is a prerequisite for any decision that gate runs on.

### Load

```bash
make build-policy-index-ato-reasoner
# equivalent to:
uv run python -m policy_rag_gate.corpus_loader --reasoner-id ato-reasoner
```

The loader resolves the corpus directory from the composed plugin whose `reasoner_id` matches (`ato_reasoner/corpus/`), else from `<POLICY_RAG_GATE_ASSETS_ROOT>/<reasoner_id>/corpus` for a reasoner registered without a plugin; pass `--corpus-dir` to override. It refuses a `reasoner_id` the ledger does not register. Expected output: progress lines per document, then a summary. Typical run: 32 documents → ~215 chunks. The first run is slower (model download on first use); subsequent runs use the cached model.

If the Elasticsearch index was created before `reasoner_id` was mapped as `keyword`, the loader logs that it is recreating the index. That drops every reasoner's BM25 documents, so run `make build-policy-index` afterward to reload all of them.

### Verify

```bash
# PostgreSQL — chunk count per policy document
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c "SELECT policy_id, COUNT(*) AS chunks FROM policy_rag.policy_chunks GROUP BY policy_id ORDER BY chunks DESC LIMIT 10;"

# PostgreSQL — total chunk count
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c "SELECT COUNT(*) FROM policy_rag.policy_chunks;"
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
uv run python -m policy_rag_gate.corpus_loader --reasoner-id ato-reasoner

# Wipe this reasoner's chunks from both stores, then reload
uv run python -m policy_rag_gate.corpus_loader --reasoner-id ato-reasoner --wipe
```

---

## 3. Synthetic Event Generator

### List available scenarios

```bash
uv run python -m ato_reasoner.generator list
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
uv run python -m ato_reasoner.generator run --scenario baseline_normal --count 5 --dry-run
```

Check the output:
- `scenario_tag` — matches the scenario name (stripped by ingestion consumer before pipeline sees it)
- `device_fingerprint` — colon-separated tokens (e.g. `abc123:def456:...`)
- `geolocation.country` — should be consistent across events for STATIC geo scenarios
- `outcome` — weighted by scenario config

```bash
# Compare credential stuffing — multiple countries, rotating devices, failures
uv run python -m ato_reasoner.generator run --scenario credential_stuffing_burst --count 10 --dry-run

# Adversarial probe — check user_agent for injection payload
uv run python -m ato_reasoner.generator run --scenario adversarial_probe --count 3 --dry-run
```

### Publish to Redis

```bash
# Push 20 baseline events to the stream
uv run python -m ato_reasoner.generator run --scenario baseline_normal --count 20
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

### Decide the stream through the ledger

`python -m ato_reasoner consume` reads the stream through the consumer and decides each accepted event with `run_ato_decision`, the pipeline ATO's service route runs. It submits to the ledger `ATO_LEDGER_URL` names (with `ATO_LEDGER_KEY`), or, with no URL, to a ledger composed in its own process (the compose stack must be up).

```bash
# Publish ten logins, then decide them (one JSON line per decision on stdout,
# the run's counts on stderr)
make scenario-account_takeover
uv run python -m ato_reasoner consume --max-events 10

# Or against the ledger service, until interrupted (Ctrl-C finishes the
# messages already read; a second Ctrl-C stops at once)
ATO_LEDGER_URL=http://localhost:8000 ATO_LEDGER_KEY="$ATO_KEY" \
  uv run python -m ato_reasoner consume
```

A new consumer group starts at the beginning of the stream, so events left from earlier runs are read first (and dead-lettered as late once they are an hour old). A login the ledger refuses (a 422 or 413 about that submission) is dead-lettered as `ledger_refused` and the run goes on. Any other failure of a decision (a wrong or missing `ATO_LEDGER_KEY`, an unregistered reasoner, the ledger failing or unreachable) is dead-lettered as `pipeline_error` and stops the run with exit status 1; the rest of that batch stays pending in the group (`XPENDING`), and the next run with the same `--consumer` decides it first. If Redis itself is down, nothing can be dead-lettered: the run stops with exit status 1 and names the error. A second Ctrl-C abandons the login being decided: it was already acknowledged, so it is neither decided nor dead-lettered (its payload stays in the stream).

### Run the consumer interactively (Python REPL)

```bash
uv run python
```

```python
import redis
from ato_reasoner.ingestion.consumer import IngestionConsumer

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

Each DLQ entry has: `reason` (schema_error / lateness / duplicate from the consumer's checks; ledger_refused / pipeline_error for an accepted event whose decision failed under `consume`), `detail`, `original_id`, and `event_id` (when available).

### Test deduplication manually

Push the same event twice, then consume once — first delivery accepted, second dead-lettered:

```python
import json
import redis
from ato_reasoner.ingestion.consumer import IngestionConsumer
from ato_reasoner.events import (
    AuthMethod, AuthOutcome, Geolocation, LoginEvent,
)
from datetime import datetime, UTC

r = redis.Redis(decode_responses=True)

event = LoginEvent(
    event_id="manual-dedup-test-001",
    timestamp=datetime.now(UTC),
    account_id="acc-manual-001",
    session_id="sess-001",
    ip_address="203.0.113.1",
    geolocation=Geolocation(latitude=37.77, longitude=-122.42, country="US",
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

Query the RAG policy gate's retriever directly, after loading a corpus.

### Interactive retrieval (Python REPL)

```bash
uv run python
```

```python
import psycopg
from elasticsearch import Elasticsearch
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer
from app.settings import FrameworkSettings
from policy_rag_gate.retriever import PolicyRetriever

pg = psycopg.connect(FrameworkSettings().postgres_dsn)  # the POSTGRES_* settings
register_vector(pg)
es = Elasticsearch("http://localhost:9200")
model = SentenceTransformer("all-MiniLM-L6-v2")
retriever = PolicyRetriever(pg, es, model)
RID = "ato-reasoner"  # tenant filter: always pass it, as the pipeline does
```

```python
# Dense search — MFA query
results = retriever.dense_search("multi-factor authentication TOTP requirement", k=5, reasoner_id=RID)
for s in results:
    print(f"{s.document_id:30s} | {s.section_path:40s} | {s.relevance_score:.4f}")
```

```python
# Sparse BM25 search (returns hits only if reasoner_id is mapped as keyword)
results = retriever.sparse_search("account lockout authentication failure threshold", k=5, reasoner_id=RID)
for s in results:
    print(f"{s.document_id:30s} | {s.section_path}")
```

```python
# Hybrid retrieval (dense + BM25 → RRF → rerank), as the policy gate calls it.
# In the pipeline the query string comes from ato_reasoner.retrieval_query.build_ato_query.
snippets, path = retriever.retrieve(
    "impossible travel new device failed password login from Paris",
    k=5,
    reasoner_id=RID,
    jurisdictions=["US_FEDERAL", "INTERNAL"],
)
print(f"Retrieval path: {path}")
for s in snippets:
    print(f"  {s.document_id:30s} | {s.relevance_score:.4f} | {s.section_path}")
```

Expected: `path = "rrf_only"` here, because this retriever was built without a cross-encoder (the gate's own `policy_rag_gate.plugin.open_retriever` passes one, giving `"reranked"` unless the latency budget is exceeded). Geographic risk and ATO detection policy chunks should surface.

---

## 6. Full Test Suite

```bash
# Unit tests (no Docker required)
make test

# Integration tests (requires docker compose up -d and make prepare-account_takeover)
make test-integration

# Smoke tests (bundle replay and the ledger API, plus the ATO scenario smoke run)
make test-smoke

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
# CASCADE also empties decision_resolution_attempts (it references the bundles).

# The identity graph: clear the entity journal and its projection together
# (the projection is rebuilt from the journal and the bundles, so emptying one
# alone leaves it inconsistent). Registration snapshots
# (decisionledger.reasoner_registrations) can stay: startup re-writes every
# served version.
docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c "TRUNCATE decisionledger.entity_events, decisionledger.entity_membership;"

docker compose exec postgres psql -U decisionledger -d decisionledger \
  -c "TRUNCATE policy_rag.policy_chunks;"
# Note: this empties the RAG policy gate's dense store only; reload each corpus
# with the loader's --wipe, which clears that reasoner's chunks from both stores
# (Postgres and the ES index)
```

### Full environment reset

```bash
docker compose down -v     # destroys all volumes
docker compose up -d       # fresh start
make prepare-reasoners     # reload every reasoner's corpus (+ train missing models)
```

---

## Scorer and Eval

```bash
make train-account_takeover        # retrain the XGBoost scorer (artifact + model card + CSVs)
make eval-model-account_takeover   # re-evaluate the scorer on a fresh-seed test set
make capture-baselines-account_takeover   # re-capture faithfulness fixtures from live gate runs
make eval                          # every reasoner's eval, then every gate's (slow, paid)
```

The latest committed eval report (`outputs/eval/eval-report-v2.json`, 2026-10-11) measures the `ato-v3` prompt and predates the split into the framework's and the RAG policy gate's harnesses; `eval-report-v1.json` is kept as history. New runs are staged under `outputs/stage/eval/reasoner/<reasoner_id>/` and `outputs/stage/eval/gate/policy/<reasoner_id>/`; see [Evaluation](../design/evaluation.md).

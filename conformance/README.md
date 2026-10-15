# DecisionLedger conformance suite

A pytest suite that checks a running DecisionLedger instance against the
ledger's HTTP contract (DR-31) — status codes, receipts, problem documents
and their stable codes, authentication and tenant isolation, batch
semantics, the subject timeline, replay, registrations, gates, and human
resolutions. It talks HTTP only (`httpx`) and imports no DecisionLedger
code, so a reasoner team can run it against any instance instead of reading
the implementation.

The payloads under `examples/` are the example reasoner's
(`reasoners/example`), one per route:

| File | Route | What it exercises |
| --- | --- | --- |
| `record.json` | `POST /api/v1/decisions` | A record under the `low-score` bypass rule (ALLOW) |
| `gate.json` | `POST /api/v1/decisions` | A gate request for the RAG policy gate |
| `held-record.json` | `POST /api/v1/decisions` | A record whose cited rule does not hold (HOLD) |
| `unregistered-rule.json` | `POST /api/v1/decisions` | A citation of no registered rule (422) |
| `batch.ndjson` | `POST /api/v1/decisions:batch` | Three records and one gated line (rejected) |
| `resolution.json` | `POST /api/v1/decisions/{id}/resolutions` | An operator's resolution of a held decision |

Each test sends a copy with a fresh `event_id` and subject (`entity_id`),
so runs never collide.

## The instance

The instance must register the example reasoner and give it an API key:

- a checkout of this repository after `uv sync` installs the example
  reasoner's plugin — the usual way; or
- an instance without that plugin registers it from `registrations.yaml`
  (`DECISIONLEDGER_REGISTRATIONS=conformance/registrations.yaml`; not both —
  a reasoner registered twice refuses startup). This works only on an
  instance that does not compose the RAG policy gate: that gate serves only
  reasoners whose prompts and corpus it loaded from their plugins, so it
  cannot serve a reasoner registered by file, and the instance refuses to
  start with one bound to it (rather than refusing every submission).

The example binds the RAG policy gate (advisory). Its gated example needs
the example corpus indexed (`make prepare-example`) for a meaningful
verdict; the suite checks the contract — 201, the gate's record — not the
verdict. An instance may mandate further gates for every reasoner
(`DECISIONLEDGER_MANDATORY_GATES`); the suite tolerates them, but a
mandated gate must accept the example's decisions for the write tests to
pass.

## Run

```bash
# The instance (keys are any secrets you choose):
DECISIONLEDGER_API_KEYS="example-reasoner:ex-key,ato-reasoner:ato-key" \
DECISIONLEDGER_OPERATOR_KEY=op-key \
uv run uvicorn --factory app.main:create_app --port 8000

# The suite:
DECISIONLEDGER_URL=http://localhost:8000 \
DECISIONLEDGER_KEY=ex-key DECISIONLEDGER_OTHER_KEY=ato-key \
DECISIONLEDGER_OPERATOR_KEY=op-key \
make conformance
```

| Variable | Needed for |
| --- | --- |
| `DECISIONLEDGER_URL` | everything (the suite skips without it) |
| `DECISIONLEDGER_KEY` | the example reasoner's key: every write and read |
| `DECISIONLEDGER_OTHER_KEY` | another reasoner's key: 403 and tenant isolation (skipped without it) |
| `DECISIONLEDGER_OPERATOR_KEY` | the operator key: resolutions (skipped without it) |

The suite is not part of `make test` (it needs a live instance); it is
marked `conformance`.

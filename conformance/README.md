# DecisionLedger conformance suite

A pytest suite that checks a running DecisionLedger instance against the
ledger's HTTP contract (DR-31, DR-32) — status codes, receipts, problem
documents and their stable codes, authentication and tenant isolation, batch
semantics, the subject timeline, replay, registrations, gates, human
resolutions, identity resolution, and the identity layer every decision
records. It talks HTTP only (`httpx`) and imports no DecisionLedger
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

| Test file | What it checks |
| --- | --- |
| `test_health.py` | `GET /health` answers without a key |
| `test_decisions.py` | A record is written (201, receipt, `Location`); the bundle at that location is schema version 2 and holds the submission as sent; replay reproduces it; a gated submission runs the policy gate; an unknown decision is 404 |
| `test_refusals.py` | A body that is not a submission, a record without its citation, and an unregistered bypass rule are 422; no key or an unknown key is 401; another reasoner's key cannot write or read; the operator key writes no decision; errors outside the API are problem documents too |
| `test_batch_and_entities.py` | A batch records its records and rejects its gated line (`BATCH_RECORD_ONLY`); an empty batch is 422; the timeline lists the recorded decisions and needs a UUID |
| `test_resources_and_resolutions.py` | The registrations resource serves the version every receipt names; the gates resource describes each composed gate; the operator resolves a held decision once, and only a held or challenged one |
| `test_identity.py` | `GET /api/v1/identity/resolve` is 404 `IDENTITY_UNRESOLVED` for a subject nobody knows and 422 without both parameters; a decision that names a native subject records it in the identity's provenance, and one that names none is recorded too, both unresolved under the reasoner's own `entity_id`; the timeline says which subject each decision is about, and `include_related` with a type nothing emits adds nothing |

`test_identity.py` is deployment-agnostic: its native namespaces are fresh
random names that no identity resolver matches and no registration declares,
so it passes on any instance, with or without an enterprise resolver.

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

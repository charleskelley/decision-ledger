# DecisionLedger conformance suite

A pytest suite that checks a running DecisionLedger instance against the
ledger's HTTP contract (DR-31, DR-32, DR-33) — status codes, receipts,
problem documents and their stable codes, authentication and tenant
isolation, batch semantics, the subject timeline, replay, registrations
(read and written over HTTP), gates, human resolutions, identity
resolution, and the identity layer every decision records. It talks HTTP only (`httpx`) and imports no DecisionLedger
code, so a reasoner team can run it against any instance instead of reading
the implementation.

The payloads under `examples/` are the example reasoner's
(`reasoners/example`), one per route, except `registration.json`, the probe
reasoner the registration suite writes:

| File | Route | What it exercises |
| --- | --- | --- |
| `record.json` | `POST /api/v1/decisions` | A record under the `low-score` bypass rule (ALLOW) |
| `gate.json` | `POST /api/v1/decisions` | A gate request for the RAG policy gate |
| `held-record.json` | `POST /api/v1/decisions` | A record whose cited rule does not hold (HOLD) |
| `unregistered-rule.json` | `POST /api/v1/decisions` | A citation of no registered rule (422) |
| `batch.ndjson` | `POST /api/v1/decisions:batch` | Three records and one gated line (rejected) |
| `resolution.json` | `POST /api/v1/decisions/{id}/resolutions` | An operator's resolution of a held decision |
| `registration.json` | `PUT /api/v1/reasoners/{id}` | The probe reasoner `conformance-reasoner`: no gate, one bypass rule |

Each test sends a copy with a fresh `event_id` and subject (`entity_id`),
so runs never collide. For that reason the payloads name no native subject,
although the example reasoner names `(example-widget, widget_id)` on every
submission: a fresh `entity_id` would contradict that namespace's
derivation (422 `NATIVE_SUBJECT_MISMATCH`). Drop `native_subject` when
regenerating them from `python -m example_reasoner sample`.

| Test file | What it checks |
| --- | --- |
| `test_health.py` | `GET /health` answers without a key |
| `test_decisions.py` | A record is written (201, receipt, `Location`); the bundle at that location is schema version 2 and holds the submission as sent; replay reproduces it; a gated submission runs the policy gate; an unknown decision is 404 |
| `test_refusals.py` | A body that is not a submission, a record without its citation, and an unregistered bypass rule are 422; no key or an unknown key is 401; another reasoner's key cannot write or read; the operator key writes no decision; errors outside the API are problem documents too |
| `test_batch_and_entities.py` | A batch records its records and rejects its gated line (`BATCH_RECORD_ONLY`); an empty batch is 422; the timeline lists the recorded decisions and needs a UUID |
| `test_resources_and_resolutions.py` | The registrations resource serves the version every receipt names; the gates resource describes each composed gate; the operator resolves a held decision once, and only a held or challenged one |
| `test_identity.py` | `GET /api/v1/identity/resolve` is 404 `IDENTITY_UNRESOLVED` for a subject nobody knows and 422 without both parameters; a decision that names a native subject records it in the identity's provenance, and one that names none is recorded too, both unresolved under the reasoner's own `entity_id`; the timeline says which subject each decision is about, and `include_related` with a type nothing emits adds nothing |
| `test_registrations.py` | With the operator key, `PUT /api/v1/reasoners/{id}` registers the probe reasoner (201 the first time, 200 after) as `source: api`; an unchanged registration keeps its version; a changed one gets a new version and the old one stays readable; a reasoner key is 403 `OPERATOR_REQUIRED` and no key 401; a plugin's or the file's reasoner is 409 `REGISTRATION_SOURCE_IMMUTABLE`; a body that is not a registration for the path is 422 `REGISTRATION_INVALID`, a mandatory gate the instance does not compose 422 `GATE_BINDING_REFUSED`, a native namespace declared differently 422 `NATIVE_NAMESPACE_CONFLICT`, and a refused write stores nothing |

`test_registrations.py` registers one probe reasoner per instance, not per
run: registrations cannot be deleted over the API (DR-33), so the probe
keeps the id `conformance-reasoner` and every refused write uses a fresh id
(a refusal stores nothing). The probe binds no gate, so an instance that
mandates a gate refusing the empty configuration refuses it. Do not run two
registration suites against one instance at once: both write the probe.

`test_identity.py` is deployment-agnostic: its native namespaces are fresh
random names that no identity resolver matches and no registration declares,
so it passes on any instance, with or without an enterprise resolver.

## The instance

The instance must register the example reasoner and give it an API key:

- a checkout of this repository after `uv sync` installs the example
  reasoner's plugin — the usual way; or
- an instance without that plugin (`DECISIONLEDGER_REASONERS` naming other
  reasoners only) registers it from `registrations.yaml`
  (`DECISIONLEDGER_REGISTRATIONS=conformance/registrations.yaml`), or over
  HTTP: `PUT /api/v1/reasoners/example-reasoner` with the operator key and
  the same registration as JSON. One source per reasoner: a reasoner
  registered twice refuses startup.

The example binds the RAG policy gate. An instance that composes that gate
serves a reasoner registered without a plugin from the gate's asset root
(`POLICY_RAG_GATE_ASSETS_ROOT`): put the example's `prompts/` and `corpus/`
(from `reasoners/example/src/example_reasoner/`) under
`<root>/example-reasoner/`, and load the corpus as `example-policy` (the
corpus the registration's binding names, DR-34) before the registration is
admitted:

```bash
POLICY_RAG_GATE_ASSETS_ROOT=/path/to/assets \
uv run python -m policy_rag_gate.corpus_loader --corpus-id example-policy \
    --corpus-dir /path/to/assets/example-reasoner/corpus
```

Otherwise the instance refuses the registration (at startup for the file,
422 `GATE_BINDING_REFUSED` for a `PUT`), naming the gate.

A registration written over HTTP is kept by that database: every process
that starts the ledger on it (a restart, `LocalClient.from_settings`, an eval
driver, the smoke tests) serves it and admits it again, so it needs the same
`DECISIONLEDGER_REASONERS` and `POLICY_RAG_GATE_ASSETS_ROOT` (put them in
`.env`). Registrations cannot be deleted over the API; to return a
development database to the plugin setup, delete the row
(`delete from decisionledger.reasoner_registry where reasoner_id =
'example-reasoner'`; see [Registration](../docs/design/registration.md)),
or use a throwaway database volume for the over-HTTP setup.

The example binds the RAG policy gate (advisory) to the `example-policy`
corpus, and an instance refuses to start until every corpus a registration
it serves binds is loaded. So load the corpora first: `make prepare-example`
for the example's, or `make prepare-reasoners` for every installed reasoner's
(with `DECISIONLEDGER_REASONERS` blank the instance also serves ATO, whose
binding names `ato-policy`). The suite checks the gated example's contract —
201, the gate's record — not its verdict. An instance may mandate further gates for every reasoner
(`DECISIONLEDGER_MANDATORY_GATES`); the suite tolerates them, but a
mandated gate must accept the example's decisions for the write tests to
pass.

## Run

```bash
# Every corpus the instance's registrations bind, loaded before it starts:
make prepare-reasoners

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
| `DECISIONLEDGER_OPERATOR_KEY` | the operator key: resolutions and registrations (skipped without it) |

The suite is not part of `make test` (it needs a live instance); it is
marked `conformance`.

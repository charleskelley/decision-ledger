# mock-stream-reasoner

The real-time event scorer pattern for DecisionLedger, packaged as a uv
workspace member. Import package: `mock_stream_reasoner`; distribution:
`mock-stream-reasoner`; `reasoner_id`: `mock-stream-reasoner`.

It is one of the reference reasoner patterns (DR-35): the repository carries
one mock reasoner per valid reasoner pattern, so the framework's contracts
are demonstrated and tested in place. A mock demonstrates contracts; it is
not a product. The domain is imaginary: accounts send transfers on a
stream, and a transparent heuristic scores each transfer against the
account's recent activity. The mock records the clear cases under its
bypass rules, asks the ledger to gate the middle through the RAG policy
gate, and runs as its own service: the ledger never imports it, whichever
way it is registered (its plugin, a registration file, or a `PUT`). The
design page is `docs/design/reasoners/patterns.md`.

| Module | What it shows |
| --- | --- |
| `events.py` | The mock's own event (`TransferEvent`: `event_id`, an aware `timestamp`, `account_id`, `amount` > 0; strict, unknown fields refused). It names the account as its native subject, `(mock-stream-account, account_id)`, and derives `entity_id` (a UUID5 of `account_id`, type `account`) under that declared namespace. |
| `state.py` | `AccountWindows`: per-account state on event time — a sliding window (10 minutes), the baseline, and the history, all read from the account's transfers timestamped before the one scored. A transfer's features are read before it joins the state; `thin_history` holds while the account has fewer than three transfers before it. `reset()` forgets every account. |
| `scorer.py` | A pure scorer: `activity_score = logistic(-4 + 0.9·window_events + 1.6·ln(1 + amount_ratio) + 1.2·thin_history)`, with each term's contribution. |
| `source.py` | `EventSource`: an async in-memory stream standing in for a broker, with validation, dedup by `event_id`, a watermark and lateness bound, and dead letters. |
| `assembler.py` | The handoff: records a transfer under the bypass rule that holds, or asks the ledger to gate it with no proposal. Every submission addresses the policy gate (template `mock-stream-v1`, its variables, a retrieval query, jurisdiction `GLOBAL`). |
| `pipeline.py` | `decide_transfer` (score, assemble, submit through any `LedgerClient`) and `StreamProcessor`, the consumer loop that dead-letters what it cannot decide and never stops on one event. |
| `registry.py` | The registration: the RAG policy gate (advisory, bound to the corpus `mock-stream-practices` at tier 2, `min_provenance: pinned_version`), the `steady-activity` (< 0.2 → record ALLOW) and `activity-surge` (> 0.9 → record BLOCK) bypass rules, the `mock-stream-account` native namespace, and Tier 3 on `thin_history`. `registration_yaml()` writes the registration file. |
| `plugin.py` | What a ledger deployment that installs the member discovers: the registration, `assets/prompts/`, and `assets/corpus/`. |
| `settings.py` | `MockStreamSettings` (`MOCK_STREAM_` prefix): the ledger URL (required) and key, the window, and the lateness bound. |
| `api.py` | The service's routes under `/api/v1/stream`. |
| `app.py` | `create_app`: the mock's FastAPI service. Its lifespan opens the stream and the state, starts the consumer task, and submits over HTTP (`HttpClient`). |
| `replay.py` | `synthetic_stream` (deterministic for a seed and a run tag, faults included) and `replay_events`, which posts a stream to the service and waits until it has caught up. |
| `testing.py` | `ScriptedPolicyGate` and `ScriptedPolicyGatePlugin`: a scripted `policy` gate double (a fixed verdict, binding admission in the real gate's shapes) for the tests and the eval. It calls no model and imports no gate package. |
| `eval/` | The `EvalAdapter`, an in-process driver, and its datasets (consistency; robustness schema failure and fallback). |
| `assets/` | `prompts/mock-stream-v1.yaml` (immutable; a change is a new file) and `corpus/` (two practice notes, `MS-PRAC-NEW-ACCOUNT` and `MS-PRAC-VELOCITY`, jurisdiction `GLOBAL`), laid out as the mock's directory under an asset root. |
| `__main__.py` | The CLI: `serve`, `replay-events`, `registration-yaml`. |

`registration.yaml` at the member root is the registration file, generated
from `registry.py` (`make registration-mock_stream`); do not edit it by
hand: a test holds the two to one `registration_version`.

## How it plugs in

Two entry points in `pyproject.toml`, both named after the `reasoner_id`:

| Group | Target | Consumed by |
| --- | --- | --- |
| `decisionledger.reasoners` | `mock_stream_reasoner.plugin:PLUGIN` | `app.plugins.discover_plugins` (the registration; the policy gate reads `prompts_dir` and `corpus_dir`) |
| `decisionledger.eval_adapters` | `mock_stream_reasoner.eval.adapter:ADAPTER` | `eval.adapter.load_eval_adapter` (eval harness) |

The ledger runs none of this package's code (DR-31). Unlike the example,
the mock never runs the ledger in its own process: its service always
submits over HTTP, to `MOCK_STREAM_LEDGER_URL` with
`MOCK_STREAM_LEDGER_KEY`, which is the mock's key in the ledger's
`DECISIONLEDGER_API_KEYS` (`mock-stream-reasoner:<key>`).

Infrastructure: none of its own. Its gated transfers go to the RAG policy
gate, which needs the corpus loaded as `mock-stream-practices` before a
ledger that serves the mock starts (or before the registration is written):
the gate refuses a binding to a corpus that is not loaded.

## Three ways to register

| Source | How | `source` label |
| --- | --- | --- |
| Plugin (the default) | The root `reasoners` group installs the member, so a ledger with `DECISIONLEDGER_REASONERS` blank composes it | `plugin:mock_stream_reasoner.plugin` |
| File | `DECISIONLEDGER_REGISTRATIONS=reasoners/mock_stream/registration.yaml`, with the plugin excluded: `DECISIONLEDGER_REASONERS=ato-reasoner,example-reasoner` | `file:reasoners/mock_stream/registration.yaml` |
| API | `PUT /api/v1/reasoners/mock-stream-reasoner` with the operator key and the registration as JSON, the plugin excluded the same way | `api` |

All three serve the same registration and the same `registration_version`.
Registered by file or over the API, the mock has no plugin in the ledger's
composition, so the RAG policy gate reads its prompts and corpus from the
gate's asset root (`POLICY_RAG_GATE_ASSETS_ROOT`, below).

The PUT body is the registration file's one entry as JSON:

```bash
uv run python -m mock_stream_reasoner registration-yaml \
  | uv run python -c 'import json, sys, yaml; print(json.dumps(yaml.safe_load(sys.stdin)["registrations"][0]))' \
  > /tmp/mock-stream-registration.json
curl -s -X PUT localhost:8000/api/v1/reasoners/mock-stream-reasoner \
  -H "X-DecisionLedger-Key: $DECISIONLEDGER_OPERATOR_KEY" \
  -H 'content-type: application/json' --data @/tmp/mock-stream-registration.json
# Expected: 201, "source": "api", "registration_version": "20f6c04e5eb2cef6"
```

**One source.** A reasoner has one source: a ledger that composes the
plugin or reads the file while an API row for `mock-stream-reasoner`
exists refuses to start, and a `PUT` for the mock while its plugin or the
file registers it answers `409 REGISTRATION_SOURCE_IMMUTABLE`. The mock's
eval driver composes the plugin, so `make eval-mocks` refuses too while the
row exists. To switch from the API registration back to the plugin or the
file, delete the row (a development database; the snapshots stay, so the
bundles that cite them still replay):

```sql
delete from decisionledger.reasoner_registry where reasoner_id = 'mock-stream-reasoner';
```

## The asset root

`make assets-mock_stream` copies `src/mock_stream_reasoner/assets/` to
`<ASSETS_ROOT>/mock-stream-reasoner/` (`ASSETS_ROOT` defaults to
`outputs/stage/assets`), the mock's directory under a RAG policy gate asset
root:

```text
outputs/stage/assets/
└── mock-stream-reasoner/
    ├── prompts/mock-stream-v1.yaml
    └── corpus/
        ├── new-account-transfer-practice.md
        └── transfer-velocity-practice.md
```

Point the ledger and the corpus loader at it
(`export POLICY_RAG_GATE_ASSETS_ROOT=$(pwd)/outputs/stage/assets`), and
load the corpus before you register: the gate describes every corpus a
binding names when it admits the binding. With the plugin excluded, the
loader reads the corpus from the asset root:

```bash
export DECISIONLEDGER_REASONERS=ato-reasoner,example-reasoner
make build-policy-index-mock-stream-reasoner
# = uv run python -m policy_rag_gate.corpus_loader --corpus-id mock-stream-practices --reasoner-id mock-stream-reasoner
```

## Service and command line

```bash
# The mock's service (port 8002): it submits to the ledger over HTTP
MOCK_STREAM_LEDGER_URL=http://localhost:8000 MOCK_STREAM_LEDGER_KEY=ms-key make serve-mock_stream
# = uv run python -m mock_stream_reasoner serve --port 8002   (--host 127.0.0.1 by default)

# Replay a synthetic stream of 200 events into it and print the run's report
make replay-mock_stream
# = uv run python -m mock_stream_reasoner replay-events --service-url http://localhost:8002 --count 200

# Print the stream instead (it still starts after the service's watermark
# when the service answers), then replay the file
uv run python -m mock_stream_reasoner replay-events --dry-run > /tmp/stream.ndjson
uv run python -m mock_stream_reasoner replay-events --file /tmp/stream.ndjson

# Print the registration file a ledger reads
uv run python -m mock_stream_reasoner registration-yaml
```

The same CLI is installed as the `mock-stream-reasoner` console script.
`serve` refuses invalid settings before it starts the server (a blank
`MOCK_STREAM_LEDGER_URL`, or one that is not http(s), exits 2 with the
reason).

| `replay-events` flag | Default | Meaning |
| --- | --- | --- |
| `--service-url` | `http://localhost:8002` | The mock's service |
| `--count` | 200 | Payloads of the synthetic stream, faults included (at least 45) |
| `--seed` | 7 | The stream's shape for a run tag (amounts and spacing) |
| `--run-tag` | random | Prefix of this run's event and account ids, so runs never collide |
| `--start` | after the service's watermark | Event time of the first transfer (timezone-aware ISO 8601); by default one minute after the service's watermark, so a second replay is not dead-lettered as late (now, when the service holds none, or when a dry run cannot reach it; a stream replayed with `--file` keeps the times it was written with) |
| `--file` | none | Replay this NDJSON stream instead of a synthetic one |
| `--chunk` | 50 | Payloads per `POST /api/v1/stream/events` |
| `--timeout` | 600 | Seconds to wait for the service to catch up |
| `--decisions-out` | none | Write the run's decisions the service still lists here, as NDJSON |
| `--dry-run` | off | Print the stream as NDJSON and post nothing |

The report lists what was published, the source's outcomes, the decisions
by mode, action, and decision-level rule, every gated decision with its
`decision_id`, a sample of the recorded ones, and the dead letters. A
replay that cannot finish (the service unreachable, a timeout, an
unreadable file) exits 1.

| Route | What it does |
| --- | --- |
| `POST /api/v1/stream/events` | Offer one event object or an array (at most 10,000). Each is validated, deduplicated, and checked for lateness at once; the answer says what the source did with each (`accepted`, `duplicate`, or `dead_lettered` with the reason). A body that is not JSON, or not an object or an array, answers 422. |
| `GET /api/v1/stream/stats` | The source's and the consumer's counters, `pending`, `accounts`, the `watermark`, and `caught_up` |
| `GET /api/v1/stream/decisions?limit=100` | The most recent decisions, oldest first (the service keeps its last `MOCK_STREAM_HISTORY`, 1,000 by default; the ledger holds every one) |
| `GET /api/v1/stream/dead-letters` | Every dead letter, oldest first |
| `POST /api/v1/stream/reset` | Forget the per-account state, the dedup set, and the watermark |
| `GET /health` | Liveness |

The service is a local demonstration surface: its routes take no key. On
shutdown the stream closes and the consumer finishes the events already
accepted (for at most 30 seconds).

## Stream semantics

- **Validation.** A payload that is not a `TransferEvent` is dead-lettered
  as `malformed`, with the validation errors.
- **Dedup.** A second delivery of an `event_id` the source has seen is
  dropped and counted (`duplicates`), not dead-lettered: a broker delivers
  at least once, and the source makes it effectively once. The check comes
  before the lateness check; the set remembers the last 100,000 ids.
- **Lateness and the watermark.** The watermark is the latest event time
  the source has accepted. An event older than the watermark by more than
  the allowed lateness (5 minutes; `MOCK_STREAM_ALLOWED_LATENESS_SECONDS`)
  is dead-lettered as `late`: the windows could no longer place it.
- **Decisions.** The consumer decides accepted events in arrival order.
  What the ledger refuses (a 4xx) is dead-lettered as `rejected`, with the
  problem's code; a transport error or a 5xx as `unreachable`; anything
  else as `failed`. The consumer never stops on one event, and nothing the
  stream carried is lost silently.
- **Per-account state on event time.** A transfer's features read only
  the account's transfers timestamped before it: the window those within
  10 minutes of it (`MOCK_STREAM_WINDOW_SECONDS`), the baseline and the
  history all of them. Precursors that arrived in any order give it the
  same features; a precursor that arrives after it (within the lateness
  bound) counts for the transfers scored after that, never for one already
  scored. The service keeps each account's transfers whole for the window
  plus the lateness bound, so every accepted transfer is scored exactly.
  The state learns a transfer when it is read, before the ledger answers:
  the transfer happened whatever the ledger decides.
- **Reset.** The state lives in the process: a restarted service starts
  with every account fresh. `POST /api/v1/stream/reset` (and the eval
  driver's `reset_state`) forgets the windows, the dedup set, and the
  watermark; the dead letters and the counters are kept.

## The default replay

`make replay-mock_stream` against a fresh service publishes 200 payloads.
For every seed and run tag the stream has the same shape:

| What | Count |
| --- | --- |
| Accepted | 195 |
| Duplicates (dropped and counted) | 3 |
| Dead-lettered `late` | 1 |
| Dead-lettered `malformed` | 1 |
| Recorded ALLOW under `steady-activity` | 182 |
| Recorded BLOCK under `activity-surge` (the end of a burst) | 3 |
| Gated | 10 |

Of the ten gated transfers, three are the large first transfers of fresh
accounts (account ids containing `-fresh-`): their history is thin, so Tier
3 holds each for review (HOLD, `decision_level_rule: tier3_novel_entity`)
on a real gate verdict. The other seven (four transfers in the middle of
the burst, and three established accounts' transfers six times their
baseline) are decided by the gate's verdict. Against a ledger that composes
the RAG policy gate, the ten gated decisions are live model calls
(`OPENAI_API_KEY`); a gate that fails is held by Tier 1 instead of Tier 3.

## Make targets (run from the repo root)

| Target | What it does |
| --- | --- |
| `make build-policy-index-mock-stream-reasoner` | Load the practice notes into the RAG policy gate as `mock-stream-practices`, the corpus the mock's binding names (from the composed plugin, else the asset root) |
| `make prepare-mock_stream` | The same (the scorer needs no training); part of `make prepare-reasoners` |
| `make eval-mock_stream` | The framework eval harness with this adapter and the scripted policy double; part of `make eval-mocks`, never `make eval` |
| `make serve-mock_stream` | Run the mock's service (port 8002; `PORT=` overrides) |
| `make replay-mock_stream` | Replay a synthetic stream into it (`COUNT=200`, `SERVICE_URL=http://localhost:8002`) |
| `make registration-mock_stream` | Regenerate `registration.yaml` from `registry.py` |
| `make assets-mock_stream` | Copy `assets/` to `<ASSETS_ROOT>/mock-stream-reasoner/` (`ASSETS_ROOT` defaults to `outputs/stage/assets`) |

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `MOCK_STREAM_LEDGER_URL` | blank | The ledger the service submits to; required (blank refuses to start) |
| `MOCK_STREAM_LEDGER_KEY` | blank | The mock's key on that ledger (blank: every submission answers 401 and is dead-lettered as `rejected`) |
| `MOCK_STREAM_WINDOW_SECONDS` | 600 | How far back a transfer's per-account window reaches |
| `MOCK_STREAM_ALLOWED_LATENESS_SECONDS` | 300 | How far behind the watermark an event may arrive and still be decided |
| `MOCK_STREAM_HISTORY` | 1000 | How many recent decisions the service lists (`/decisions`); a larger replay's report says how many it no longer lists (`decisions_missing`) |

Read from the environment and `.env` when the service starts (`serve`
checks them first), never at import, so the plugin and the eval adapter
import with no environment at all. `replay-events` reads none: it talks to
the service at `--service-url`.

## Eval

`make eval-mocks` (or `make eval-mock_stream`) runs the framework eval
harness with the mock's adapter: consistency on `scenarios/stream_accounts.yaml`
(`ms-steady-account` → ALLOW and `ms-surge-account` → BLOCK, each in two
precursor orderings), and robustness on `robustness/malformed.yaml` and
`robustness/fallback.yaml` (two cases each, HOLD). The driver composes the
ledger over Postgres with the mock's plugin and the scripted policy double,
so it needs `docker compose up -d` but no Elasticsearch, corpus, or model
call. The mock ships no retrieval, faithfulness, citation, or authority
datasets, so the RAG policy gate's own eval never selects it.

## Tests

`tests/` (no `__init__.py`) runs under the root `pytest` with no Docker:
the stream, the state, the scorer, the assembler, the service, the
registration file against `registry.py`, the asset-root layout, and the
acceptance tests that register the mock by file and by `PUT` with its
plugin excluded, over the in-memory ledger (`tests._memory_ledger`) and the
scripted policy double. One `@pytest.mark.integration` acceptance test runs
over the live stack (`make test-integration`, after `make
prepare-reasoners`): it points `POLICY_RAG_GATE_ASSETS_ROOT` at a temporary
copy of the assets and deletes its registry row in teardown.

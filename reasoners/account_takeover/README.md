# ato-reasoner

The account-takeover / identity-risk reference reasoner for DecisionLedger,
packaged as a uv workspace member. Import package: `ato_reasoner`;
distribution: `ato-reasoner`; `reasoner_id`: `ato-reasoner`.

It owns the full ATO domain: login-event types, Redis-backed feature
windows and ingestion stream, the XGBoost scorer (artifact under
`src/ato_reasoner/scorer/models/`), the assembler that builds its
`Submission` (recording a login under one of its bypass rules or asking the
ledger to gate it), its own FastAPI service, prompt templates
(`prompts/ato-v*.yaml`), the policy corpus (`corpus/`), the
synthetic-scenario generator, and the eval driver and datasets. The framework (`decision-ledger`) never imports or depends on it.

`registry.py` holds `ATO_REGISTRATION`, the data the ledger reads:

- the RAG policy gate (`policy`), bound advisory: it runs when ATO asks for
  a gate;
- two bypass rules (DR-30), `allow-band` (`risk_score < 0.20` → record
  ALLOW) and `block-band` (`risk_score > 0.95` → record BLOCK), version `1`;
  every other login is gated. The thresholds live here and nowhere in the
  framework;
- `recordable_actions` ALLOW and BLOCK;
- the native namespace `ato-account` (subject type `account`, the UUID5
  namespace ATO's account ids derive under);
- the enforcement profile: `novel_entity_feature="sparse_history"` (Tier 3,
  on gated logins) and the US vs. EU `jurisdiction_conflict_groups` (Tier 5).

The active gate template is `ato-v3` (v2's user text plus a `system:` key
carrying ATO's system message); `ato-v1`/`ato-v2` stay in `prompts/`,
since a prompt version is never modified or removed.

See DR-26 in `docs/design/decisions.md` for why the reasoner is packaged
this way, `docs/design/reasoners/account-takeover/account-takeover.md` for
how it decides, and `docs/design/reasoners/reasoner-handoff.md` for the
submission contract and the checklist to copy when writing another reasoner.

## How it plugs in

Two entry points in `pyproject.toml`, both named after the `reasoner_id`:

| Group | Target | Consumed by |
| --- | --- | --- |
| `decisionledger.reasoners` | `ato_reasoner.plugin:PLUGIN` | `app.plugins.discover_plugins` (the registration; the policy gate reads `prompts_dir` and `corpus_dir`) |
| `decisionledger.eval_adapters` | `ato_reasoner.eval.adapter:ADAPTER` | `eval.adapter.load_eval_adapter` (eval harness) |

The root workspace installs it through the `reasoners` dependency group
(a default group), so a bare `uv sync` at the repo root is enough. Entry
points refresh only on reinstall: re-run `uv sync` after editing this
`pyproject.toml`.

Infrastructure: `compose.yaml` defines the `redis` service, pulled into the
root stack by `include:` in `docker-compose.yml`. Make targets come from
`reasoner.mk`, included by the root `Makefile`.

## Runs as its own service (DR-31)

The ledger mounts no reasoner router. ATO is its own FastAPI service,
`ato_reasoner.app:create_app` (`make serve-account_takeover`, port 8001),
serving `POST /api/v1/ato/decisions`: it scores the login (Redis feature
windows, the XGBoost scorer), builds its `Submission`, and submits it
through a ledger client (`ato_reasoner.pipeline.run_ato_decision`):

- `ATO_LEDGER_URL` unset (the default): the ledger runs in ATO's process
  (`LocalClient`, composed exactly as the ledger service would be);
- `ATO_LEDGER_URL=http://localhost:8000`: ATO calls that ledger over HTTP
  (`HttpClient`) with its API key `ATO_LEDGER_KEY` (configured on the
  ledger as `DECISIONLEDGER_API_KEYS=ato-reasoner:<key>`).

The response summarizes the ledger's receipt (`decision_id`,
`decision_action`, `mode`, the cited bypass rule and whether it held, the
credited rule, each gate's outcome, and the whole decision's `latency_ms`);
the full bundle, replay, and the account's decision timeline are the
ledger's routes (`GET /api/v1/decisions/{id}`, `.../replay`,
`GET /api/v1/entities/{entity_id}/decisions`).

## Decides from its event stream

`python -m ato_reasoner consume` decides the logins on ATO's Redis stream
(`ato:login-events`, where `make scenario-account_takeover` and the
generator publish). The stream consumer (`ingestion/consumer.py`) reads
through the `ato-reasoner` consumer group, dead-letters a malformed, late
(older than an hour), or duplicate event to `ato:login-events:dlq`, and
ACKs every message it checks. Each accepted login goes through the same
`run_ato_decision` the service's route runs (`ingestion/stream.py`), to the
ledger `ATO_LEDGER_URL` names or, without one, a ledger composed in process.
A login the ledger refuses (a 422 or 413 about that submission) is
dead-lettered as `ledger_refused` and the run goes on. Any other failure of
a decision (a wrong key, an unregistered reasoner, the ledger failing or
unreachable, the scorer failing) is dead-lettered as `pipeline_error` and
stops the run with exit status 1; the rest of that batch stays pending, and
the next run decides it first. A Redis failure also stops the run with exit
status 1, with nothing dead-lettered.

```bash
make scenario-account_takeover                          # publish 10 baseline_normal logins
uv run python -m ato_reasoner consume --max-events 10   # decide them: one JSON line each
```

Without `--max-events` it runs until interrupted; Ctrl-C finishes the
messages already read (a second Ctrl-C stops at once, abandoning the login
being decided). `--stream-key`, `--group`, `--consumer`,
`--batch-size`, `--block-ms`, and `--lateness-seconds` override the
defaults.

## The subject (DR-32)

Every ATO submission names the account as its native subject,
`(ato-account, account_id)`, and sends `entity_id = uuid5(namespace,
account_id)` with `entity_type` `account` (`LoginEvent.entity_id`). With no
identity resolver installed, the ledger's passthrough derives the subject
from the declared namespace: the same `entity_id` and `entity_type` ATO's
decisions always had, recorded `unresolved` with the reason. An enterprise
resolver may map the account to the subject it belongs to; the submission
keeps ATO's own id either way.

## Make targets (run from the repo root)

| Target | What it does |
| --- | --- |
| `make train-account_takeover` | Train the XGBoost scorer (artifact + model card + CSVs) |
| `make eval-model-account_takeover` | Evaluate the trained scorer on a fresh-seed test set |
| `make scenario-account_takeover` | Publish 10 `baseline_normal` synthetic login events |
| `make build-policy-index-ato-reasoner` | Embed and index `corpus/` into the RAG policy gate's stores under `reasoner_id=ato-reasoner` (the gate's target, `gates/policy_rag/gate.mk`) |
| `make prepare-account_takeover` | Build the policy index and train the scorer if the artifact is missing |
| `make eval-account_takeover` | Run the framework eval harness (consistency, robustness, shadow) with this reasoner's adapter (slow, paid); the policy gate's own eval (`make eval-gate-policy_rag`) reads this reasoner's retrieval, faithfulness, and citation datasets |
| `make capture-baselines-account_takeover` | Re-capture faithfulness fixtures from live gate runs |
| `make serve-account_takeover` | Run ATO's own service (port 8001; `ATO_LEDGER_URL` selects an HTTP ledger) |

The aggregate root targets (`make train`, `make build-policy-index`,
`make prepare-reasoners`, `make eval`) fan out to these.

## Environment

Read by `ato_reasoner.settings.AtoSettings` (prefix `ATO_`, from `.env` or
the process environment):

| Variable | Default | Purpose |
| --- | --- | --- |
| `ATO_REDIS_URL` | `redis://localhost:6379` | Feature windows, dedup store, login-event stream |
| `ATO_SCORER_MODEL_PATH` | packaged `scorer/models/ato-v1.ubj` | Override the scorer artifact |
| `ATO_CORPUS_VERSION` | `ato-v1` | Corpus version recorded in every Decision Bundle |
| `ATO_LEDGER_URL` | blank | The ledger ATO's service submits to over HTTP; blank runs the ledger in process |
| `ATO_LEDGER_KEY` | blank | ATO's API key for that ledger (`X-DecisionLedger-Key`) |
| `ATO_REDIS_PORT` | `6379` | Host port mapping in `compose.yaml` (Compose only) |

## Tests

`tests/` here is collected by the root `pytest` run (`testpaths` includes
`reasoners/*/tests`). It is a separate tree from the framework's `tests/`
and does not use the framework's stub-reasoner fixtures.

# ato-reasoner

The account-takeover / identity-risk reference reasoner for DecisionLedger,
packaged as a uv workspace member. Import package: `ato_reasoner`;
distribution: `ato-reasoner`; `reasoner_id`: `ato-reasoner`.

It owns the full ATO domain: login-event types, Redis-backed feature
windows and ingestion stream, the XGBoost scorer (artifact under
`src/ato_reasoner/scorer/models/`), the assembler that builds its
`Submission` (recording a login under a score band or asking for the gate),
prompt templates (`prompts/ato-v*.yaml`), the policy corpus
(`corpus/`), the synthetic-scenario generator, and the eval driver and
datasets. The framework (`decision-ledger`) never imports or depends on it.

The active gate template is `ato-v3` (v2's user text plus a `system:` key
carrying ATO's system message); `ato-v1`/`ato-v2` stay registered for
historical bundles. `registry.py` declares the enforcement profile the
framework reads: `novel_entity_feature="sparse_history"` (Tier 3) and the
US vs. EU `jurisdiction_conflict_groups` (Tier 5).

See DR-26 in `docs/design/decisions.md` for why the reasoner is packaged
this way, and `docs/design/reasoners/reasoner-handoff.md` for the contract
and the checklist to copy when writing another reasoner.

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

The response summarizes the ledger's receipt; the full bundle, replay, and
the account's decision timeline are the ledger's routes
(`GET /api/v1/decisions/{id}`, `.../replay`,
`GET /api/v1/entities/{entity_id}/decisions`).

## Make targets (run from the repo root)

| Target | What it does |
| --- | --- |
| `make train-account_takeover` | Train the XGBoost scorer (artifact + model card + CSVs) |
| `make eval-model-account_takeover` | Evaluate the trained scorer on a fresh-seed test set |
| `make scenario-account_takeover` | Publish 10 `baseline_normal` synthetic login events |
| `make build-policy-index-ato-reasoner` | Embed and index `corpus/` into the RAG policy gate's stores under `reasoner_id=ato-reasoner` (the gate's target, `gates/policy_rag/gate.mk`) |
| `make prepare-account_takeover` | Build the policy index and train the scorer if the artifact is missing |
| `make eval-account_takeover` | Run the framework eval harness (consistency, robustness) with this reasoner's adapter (slow, paid); the policy gate's own eval (`make eval-gate-policy_rag`) reads this reasoner's retrieval, faithfulness, and citation datasets |
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

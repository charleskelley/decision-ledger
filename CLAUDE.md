# DecisionLedger — Claude Code Instructions

## About This Project

DecisionLedger is a decision system of record for AI systems, with a gating service (DR-27): one Decision Bundle schema, a subject-centric ledger with replay, pluggable gates, and eval-gated release. Reasoners and gates are plugins outside the framework. The reference reasoner (ATO) is a real-time account-takeover risk pipeline: an XGBoost scorer whose clear cases ATO records under its score-band bypass rules, and whose ambiguous ones it asks the ledger to gate through the RAG policy gate (an LLM), with deterministic enforcement and full audit replay.

## Critical Rules

### Git

Git commits are blocked by `.claude/hooks/block-git-commit.sh` and `.claude/settings.json` permissions. Stage changes and let the user commit. No `Co-authored-by`, `Signed-off-by`, or AI-attribution trailers in commit messages — ever. Use Conventional Commits format with scopes: `core`, `app`, `reasoners`, `ingestion`, `features`, `scorer`, `retrieval`, `policy-gate`, `enforcement`, `audit`, `eval`, `scenarios`, `infra`, `ci`, `gates`, `api`, `client`, `mcp`, `identity`, `registration`, `evidence`, `mocks`.

### Code Style

See `docs/development/style.md` for the full guide. Headline rules: Ruff-only (no black/isort/flake8/pylint), 88-col line length, Pydantic v2 with `ConfigDict(strict=True, frozen=True)` for all models in `core/`, Google-style docstrings, Python 3.11+ syntax (`list[str]`, `str | None`), American English spelling everywhere.

### The `core/` Boundary — Absolute Rule

`core/` has **zero infrastructure dependencies**. No Redis, no database drivers, no LLM SDK, no FastAPI, no HTTP clients. Only: Pydantic models, enums, protocols, pure functions, and standard library imports.

If you need to add an import to `core/`, stop and reconsider. The test is: can this code be unit-tested without Docker running? If no, it belongs in `app/`, not `core/`.

Everything in `app/`, `eval/`, `reasoners/`, and `gates/` imports from `core/`. `core/` imports from nothing internal.

### The `reasoners/` Boundary

Each reasoner is a **uv workspace member** under `reasoners/<domain>/` (src layout) that owns the **full** domain implementation — pure types (events, feature vectors, scorer outputs, policy enums), ML inference and training infrastructure, ingestion adapters, the domain pipeline orchestrator, its own FastAPI service, settings, the registration record (bypass rules, gate bindings, native namespaces), prompt templates, policy corpus, scenario generator, eval adapter/driver/datasets, and any infrastructure only it uses. It exposes its work to the framework exclusively through the `Submission` (`<pkg>/assembler.py:build_submission` → a `LedgerClient` → `app/submit.py:submit`) and is discovered through entry points, never imported by the framework (DR-26, DR-31). A reasoner that runs elsewhere needs no plugin: it registers through the `DECISIONLEDGER_REGISTRATIONS` file and submits over HTTP.

For ATO today: `reasoners/account_takeover/` is distribution `ato-reasoner`, import package `ato_reasoner`, `reasoner_id` `ato-reasoner`. It has `pyproject.toml` (deps + entry points `decisionledger.reasoners` → `ato_reasoner.plugin:PLUGIN` and `decisionledger.eval_adapters` → `ato_reasoner.eval.adapter:ADAPTER`), `ruff.toml`, `reasoner.mk` (suffixed make targets), `compose.yaml` (Redis), `tests/` (no `__init__.py`), and `src/ato_reasoner/` with `events.py`, `features.py`, `scorer/` (+ `models/ato-v1.ubj`), `feature_service.py`, `ingestion/`, `assembler.py`, `pipeline.py`, `api.py` and `app.py` (ATO's own FastAPI service), `settings.py` (`ATO_` prefix), `registry.py`, `policy.py`, `retrieval_query.py`, `plugin.py`, `prompts/`, `corpus/`, `generator/`, and `eval/`.

Reasoners import from `core/` and `app/`; `core/`, `app/`, and `eval/` never import from a reasoner. Adding a second reasoner (e.g., content moderation) means creating `reasoners/content_moderation/` with the same shape and adding it to the root `reasoners` dependency group — no framework changes required. `reasoners/example/` (`example-reasoner`, widgets) is a minimal documented reasoner; `tests/_stub_reasoner/` is a test-only reasoner that proves the framework needs neither. The four reference-pattern mocks of DR-35, `reasoners/mock_stream/`, `reasoners/mock_batch/`, `reasoners/mock_text/`, and `reasoners/mock_daily/` (`mock-<pattern>-reasoner`), are members of the same shape that the `reasoners` group installs by default and that never enter `make eval` (`make eval-mocks` runs their evals).

### The `gates/` Boundary

Each gate is a **uv workspace member** under `gates/<name>/` (src layout), discovered through the `decisionledger.gates` entry point (`app/gates.py:GatePlugin`) and installed by the root `gates` dependency group (DR-28). It owns everything it needs: its infrastructure, if any (a `compose.yaml` the root compose file includes, its own Postgres schema and DDL, a `gate.mk`), any prompt registry, corpus loader, and LLM adapters, and its own eval. The framework never imports a gate: it stores only the open `GateOutput` base (`core/gate/output.py`) plus the gate's opaque `evidence_snapshot` and `extra`, and never defines or imports a `GateOutput` subclass. Gates are reasoner-agnostic; a reasoner reaches one only through its `GateContext`, whose `gate_config` the gate's `config_model` validates at intake.

The reference gates: `rules` (`gates/rules`, package `rules_gate`; zero infrastructure; rule sets under `RULES_GATE_RULESETS_DIR`) and `policy` (`gates/policy_rag`, package `policy_rag_gate`; Elasticsearch, the `policy_rag` schema with pgvector, the LLM client). LLM SDK imports live only in `policy_rag_gate`'s `llm/` (DR-23; the CI boundary job and `tests/test_framework_boundary.py` check). `tests/_fake_gate/` is test-only.

### The Framework Boundary

The framework is `core/`, `app/`, and `eval/` — the root `decision-ledger` package. Its `[project] dependencies` and wheel never include a reasoner (or xgboost, pandas, redis). `app/` is framework runtime: `app/main.py:create_app()` discovers reasoner plugins (`app/plugins.py`, filtered by `DECISIONLEDGER_REASONERS`) and gate plugins (`app/gates.py`), `app/bootstrap.py` builds the shared infrastructure once, including the identity step (`app/identity/`). Since DR-31 (Phase 3) the ledger serves only its own routes (`app/api/`, API keys in `app/auth.py`) and runs no reasoner code: a reasoner submits over HTTP or in process through `app/client.py` (`HttpClient` / `LocalClient`), and a plugin publishes only its registration and the assets its gates read. There is **no** exception for `app/main.py`: no module under `app/`, `core/`, or `eval/` imports a reasoner or gate package or names an ATO symbol, id, template, feature, or jurisdiction (in `.py`, `.yaml`, `.sql`, or `.json`). `tests/test_framework_boundary.py` enforces this and also asserts the root pyproject declares no reasoner dependency or entry point and that each member declares its own entry points.

DecisionLedger is the framework-owned audit/governance hub. All decision bundles, replay logs, resolution attempts, registration snapshots, and the entity journal live in the `decisionledger.*` Postgres schema; decisions carry `reasoner_id` as the tenant column. The canonical DDL ships as package data in `app/audit/sql/`; Postgres is the framework's only infrastructure. The RAG policy gate retrieves through providers (DR-34) from the corpora a reasoner's registration binds it to (`PolicyBindingConfig`: corpus id, provider, authority tier); its reference provider keeps corpora, documents, and chunks in the gate's own `policy_rag` schema (DDL in its `sql/`), its ES mapping in its `es/`, and Elasticsearch in its `compose.yaml`. A corpus is loaded, before the registration that binds it, via `uv run python -m policy_rag_gate.corpus_loader --corpus-id <corpus_id> --reasoner-id <id>` (the loader reads the plugin's `corpus_dir`, else the asset root; `--corpus-dir` overrides).

## Tech Stack

- Python 3.11+, Pydantic v2, FastAPI, httpx (the framework); XGBoost (ATO's scorer); sentence-transformers (the RAG policy gate's embedder and cross-encoder)
- PostgreSQL: the framework's only infrastructure (audit, registrations, entity journal). pgvector (the RAG policy gate's `policy_rag` schema on the same server) and Elasticsearch (its BM25 index): the RAG policy gate's. Redis Streams (event queue, feature windows): ATO's
- OpenAI API (GPT-4o) for the RAG policy gate's LLM client; Anthropic for that gate's eval judges
- Docker Compose V2 (`docker compose` — no hyphen, ≥ 2.20 for `include:`) for local dev; Redis comes from ATO's `compose.yaml`, Elasticsearch from the RAG gate's
- **uv** for Python package management, venv, lockfile, and the workspace (`reasoners/*`, `gates/*`, `integrations/*` members)
- Ruff for formatting and linting; pytest for testing; GitHub Actions for CI

## Key Architecture Decisions

See `docs/design/decisions.md` (DR-N records) for the full rationale. Load-bearing invariants:

- **Replay does NOT re-invoke a gate or the LLM.** Deterministic replay re-executes enforcement against the stored gate outputs (including the logged LLM output) under the bundle's registration snapshot (`registration_version`).
- **ATO's XGBoost scorer trains on a heuristic labeling function**, not human-labeled data. The scorer triages events into confidence bands; it doesn't detect real fraud.
- **The RAG gate's cross-encoder reranking has a latency-budget bypass.** If reranking exceeds the configured timeout, fall back to RRF-only results. The path taken is recorded per corpus (`CorpusQuery.ranking_applied`) in the gate's `evidence_snapshot` in the Decision Bundle.
- **Prompt versions are immutable.** Never modify an existing prompt YAML. Create a new version file in the reasoner's `prompts/` dir (a top-level `system:` key carries its system message). Every bundle bound to the RAG gate names its template (`gate_config.template_id`), and when the gate runs its `evidence_snapshot` records the active template and version (`prompt_template_id`, `prompt_template_version`).
- **Reasoners are workspace members discovered by entry point (DR-26).** The framework never imports one; each publishes a `ReasonerPlugin` (registration incl. gate set, bypass rules, native namespaces, and enforcement profile; prompts/corpus dirs) and an `EvalAdapter`, and runs as its own service or in process through `LocalClient` (DR-31).
- **A reasoner records or gates (DR-29, DR-30).** A `record` submission cites a registered bypass rule and runs only mandatory gates; a `gate` submission runs mandatory then advisory gates. The final action is the most restrictive across the proposal and the gates, with one exception: on a gated decision about a novel entity, Tier 3 holds the advisory gates' verdicts for review (HOLD, or BLOCK when an advisory gate's own Tier 2 fired); mandatory gates, bypass floors, and the proposal still count. A failing gate fails closed (HOLD).
- **The ledger resolves every decision's subject (DR-32).** Resolver plugins register under `decisionledger.identity_resolvers`; `DECISIONLEDGER_IDENTITY_RESOLVER` names the one active resolver (blank or `passthrough` selects the passthrough built into `app/identity/`). `app/submit.py` resolves after intake validation and before the gates, under a 50 ms default budget; no match, an error, or a timeout records the subject unresolved, with the reason, and a decision is never dropped over its identity. Registrations declare their `native_namespaces`. `decisionledger.entity_events` is the append-only journal; `decisionledger.entity_membership` is a projection rebuilt from it. Framework code has no household (or any other relationship meaning): relationship types are the resolver's.

## Submission Contract

A reasoner hands the ledger one `core.submission.Submission` (wire schema `schemas/submission-v2.json`), in process or over HTTP. In a workspace reasoner it is built at a single point: `build_submission()` in `reasoners/<domain>/src/<pkg>/assembler.py`. The domain pipeline (ATO: `ato_reasoner/pipeline.py:run_ato_decision`) runs feature computation + scoring + assembly, then submits through a `LedgerClient` (`app/client.py`); its operation (`app/api/decisions.py:create_decision`, the same one `POST /api/v1/decisions` runs) calls the framework half (`app/submit.py:submit`), which validates the submission against the registry and the gates' config models, resolves its subject, runs the decision's gate set, enforces, and persists the version-2 bundle stamped with its `registration_version`. `docs/design/reasoners/reasoner-handoff.md` is the submission contract.

Invariants to know before touching features, scorer, or policy gate:

- **A `GateContext` for every bound gate is the assembler's job, and is required on ALL submissions** — recorded ones too, which still need them for shadow evaluation.
- **`feature_set` in `ReasonerContext` must be complete** — every feature the model consumed at inference time appears here. This is what makes replay self-contained, and bypass rules are evaluated on it.
- **A scorer's output type lives in the reasoner package** (ATO: `ScorerOutput` in `ato_reasoner/scorer/output.py`), never in `core/` or `app/`. The assembler translates it into `ReasonerContext` + `GateContext` and picks the mode with `core.submission.matching_bypass_rule` (the strictest registered rule that holds).
- **A native subject in a declared namespace must agree with the submission's `entity_id` and `entity_type`** (the declaration's derivation); otherwise intake answers 422 `NATIVE_SUBJECT_MISMATCH`.
- **Score bands are ATO's registered bypass rules** (`ato_reasoner/registry.py`, DR-30): `allow-band` (`< 0.20` → record ALLOW), `block-band` (`> 0.95` → record BLOCK); otherwise ATO asks for the policy gate. The numbers live nowhere in the framework.
- **Retrieval filter semantics (the RAG gate's reference provider):** `corpus_id` filters both pgvector and Elasticsearch (one query per bound corpus); `reasoner_id` is authorization (the registration's binding names the corpora), not a chunk key; `jurisdictions` filters both; `risk_tier` filters pgvector only. Scores are never compared across corpora.
- **Audit storage is framework-owned, multi-tenant by row.** All reasoners write to `decisionledger.decision_bundles` with `reasoner_id` as the tenant column; its `entity_id` column is the resolved subject. Reasoner-specific business keys (account_id, content_id) live inside the JSONB bundle, not as scalar columns.

## Common Commands

```bash
uv sync                        # Install framework + every workspace reasoner, gate, and integration
uv sync --all-groups           # Also install the docs group (mkdocs)
docker compose up -d           # Start infra (Postgres + the included ATO Redis and RAG gate Elasticsearch)
docker compose down            # Stop infra
make prepare-reasoners         # Load every reasoner's corpus (the ledger won't start until each bound corpus is loaded); train ATO's scorer if missing
make prepare-rulesets          # Stage every member's rulesets/ in outputs/stage/rulesets; export RULES_GATE_RULESETS_DIR to it before make serve
make serve                     # Run the ledger API (uvicorn --factory app.main:create_app, port 8000)
make serve-account_takeover    # Run ATO's own service (port 8001); ATO_LEDGER_URL + ATO_LEDGER_KEY submit over HTTP, else the ledger runs in process
make schemas                   # Regenerate schemas/*.json from the models (tests fail on drift)
make conformance               # HTTP contract suite against DECISIONLEDGER_URL; keys: DECISIONLEDGER_KEY (example-reasoner's), DECISIONLEDGER_OTHER_KEY, DECISIONLEDGER_OPERATOR_KEY
uv run decisionledger-mcp      # MCP server on stdio (DECISIONLEDGER_URL, DECISIONLEDGER_KEY)
make lint                      # Ruff format + lint
make typecheck                 # Pyright
make test                      # Unit tests (fast)
make test-integration          # Integration tests (requires Docker)
make test-smoke                # Smoke tests
make scenario-account_takeover # Publish 10 baseline_normal events
make eval-account_takeover     # Framework eval harness for ATO (slow); `make eval` = every reasoner, then every gate
make eval-mocks                # Every reference-pattern mock's eval (Postgres only, no model calls); never part of `make eval`
just diagrams                  # Render D2 diagrams (requires: brew install d2)
uv run python -m ato_reasoner.generator run --scenario baseline_normal --count 50
uv run python -m policy_rag_gate.corpus_loader --corpus-id ato-policy --reasoner-id ato-reasoner
```

`make help` lists the per-reasoner targets from each `reasoners/*/reasoner.mk` and the per-gate targets from each `gates/*/gate.mk`.

## Package Management

Always use `uv` — never `pip install` directly. `uv add <package>` for framework runtime deps, `uv add --dev <package>` for dev deps, `uv sync` to install/update. `uv sync` also installs the workspace reasoners, gates, and integrations (the root `reasoners`, `gates`, and `integrations` dependency groups, all default groups; `docs` is not, so `uv sync --all-groups` adds it). A member-only dependency goes in that member: run `uv add <package>` from `reasoners/<domain>/` or `gates/<name>/` (never add reasoner or gate deps to the root `[project] dependencies`). Entry points refresh only on reinstall — re-run `uv sync` after editing a member's `pyproject.toml`. The `uv.lock` lockfile is committed; do not hand-edit it.

## Writing Code

- Run `ruff check --fix` and `ruff format` on files you create or modify (the post-edit hook does this automatically).
- Type hints on all public functions; Google-style docstrings on all public classes and functions.
- For logging: structured JSON with `component`, `event_id`, `decision_id` fields; `duration_ms` for all latency measurements.
- For error handling: define explicit exception types in `core/`. Never catch bare `except:`.

## Writing Tests

Behavior-driven testing — see `docs/development/style.md` for the full philosophy. Project-specific rules:

- **`core/` contracts and enforcement routing:** test exhaustively. These are the governance guarantees.
- **LLM-touching code:** do NOT write unit tests with mocked LLM responses. The RAG policy gate's eval harness (`policy_rag_gate.eval`, `make eval-gate-policy_rag`) is the test suite for that gate; mark any test that calls a live model `@pytest.mark.evaluation`.
- **Smoke tests:** keep the `@pytest.mark.smoke` set small (eight tests today, ATO's runs once per scenario; together they cover a happy path, an error path, and a replay); a member's smoke test needs Docker only when its pipeline does.
- **Integration tests** (event in → bundle out): mark with `@pytest.mark.integration`; highest value tests in the suite.
- Test names describe behavior: `test_enforcement_routes_schema_failure_to_HOLD`, not `test_resolve_action`.

## Scope — MVP Only

Do NOT implement: drift monitoring, canary/shadow rollout, human review queue UI, GCP deployment, custom embedding training, LLM fine-tuning, multi-tenant deployment (per-tenant isolation beyond row-level `reasoner_id`), production SLA hardening, or Kafka.

## Reference Documents

- `docs/development/style.md` — Code style and testing philosophy
- `docs/development/contributing.md` — Development workflow
- `docs/design/decisions.md` — Architecture decision records (DR-N)
- `docs/design/reasoners/reasoner-handoff.md` — The submission contract, the new-reasoner and new-gate checklists (read before touching features, scorer, or policy gate)
- `docs/design/identity.md` — The identity contract, the passthrough, the entity journal, and shipping a resolver
- `docs/design/gates/` — The gate contract and how to implement a gate

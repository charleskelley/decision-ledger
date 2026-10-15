# DecisionLedger — Claude Code Instructions

## About This Project

DecisionLedger is a model-agnostic governed decision framework for AI systems. The reference implementation (ATO Reasoner) is a real-time ATO/identity risk decisioning pipeline using a hybrid ML scorer + LLM policy gate architecture with deterministic enforcement and full audit replay.

## Critical Rules

### Git

Git commits are blocked by `.claude/hooks/block-git-commit.sh` and `.claude/settings.json` permissions. Stage changes and let the user commit. No `Co-authored-by`, `Signed-off-by`, or AI-attribution trailers in commit messages — ever. Use Conventional Commits format with scopes: `core`, `app`, `reasoners`, `ingestion`, `features`, `scorer`, `retrieval`, `policy-gate`, `enforcement`, `audit`, `eval`, `scenarios`, `infra`, `ci`.

### Code Style

See `docs/development/style.md` for the full guide. Headline rules: Ruff-only (no black/isort/flake8/pylint), 88-col line length, Pydantic v2 with `ConfigDict(strict=True, frozen=True)` for all models in `core/`, Google-style docstrings, Python 3.11+ syntax (`list[str]`, `str | None`), American English spelling everywhere.

### The `core/` Boundary — Absolute Rule

`core/` has **zero infrastructure dependencies**. No Redis, no database drivers, no LLM SDK, no FastAPI, no HTTP clients. Only: Pydantic models, enums, protocols, pure functions, and standard library imports.

If you need to add an import to `core/`, stop and reconsider. The test is: can this code be unit-tested without Docker running? If no, it belongs in `app/`, not `core/`.

Everything in `app/`, `eval/`, and `reasoners/` imports from `core/`. `core/` imports from nothing internal.

### The `reasoners/` Boundary

Each reasoner is a **uv workspace member** under `reasoners/<domain>/` (src layout) that owns the **full** domain implementation — pure types (events, feature vectors, scorer outputs, policy enums), ML inference and training infrastructure, ingestion adapters, the domain pipeline orchestrator, the FastAPI router, settings, the registration record, prompt templates, policy corpus, scenario generator, eval adapter/driver/datasets, and any infrastructure only it uses. It exposes its work to the framework exclusively through the `Submission` handoff (`<pkg>/assembler.py:build_submission` → `app/submit.py:submit`) and is discovered through entry points, never imported by the framework (DR-26, DR-31).

For ATO today: `reasoners/account_takeover/` is distribution `ato-reasoner`, import package `ato_reasoner`, `reasoner_id` `ato-reasoner`. It has `pyproject.toml` (deps + entry points `decisionledger.reasoners` → `ato_reasoner.plugin:PLUGIN` and `decisionledger.eval_adapters` → `ato_reasoner.eval.adapter:ADAPTER`), `ruff.toml`, `reasoner.mk` (suffixed make targets), `compose.yaml` (Redis), `tests/` (no `__init__.py`), and `src/ato_reasoner/` with `events.py`, `features.py`, `scorer/` (+ `models/ato-v1.ubj`), `feature_service.py`, `ingestion/`, `assembler.py`, `pipeline.py`, `api.py` and `app.py` (ATO's own FastAPI service), `settings.py` (`ATO_` prefix), `registry.py`, `retrieval_query.py`, `plugin.py`, `prompts/`, `corpus/`, `generator/`, and `eval/`.

Reasoners import from `core/` and `app/`; `core/`, `app/`, and `eval/` never import from a reasoner. Adding a second reasoner (e.g., content moderation) means creating `reasoners/content_moderation/` with the same shape and adding it to the root `reasoners` dependency group — no framework changes required. `tests/_stub_reasoner/` is a test-only second reasoner that proves this.

### The Framework Boundary

The framework is `core/`, `app/`, and `eval/` — the root `decision-ledger` package. Its `[project] dependencies` and wheel never include a reasoner (or xgboost, pandas, redis). `app/` is framework runtime: `app/main.py:create_app()` discovers plugins (`app/plugins.py`, filtered by `DECISIONLEDGER_REASONERS`), `app/bootstrap.py` builds the shared infrastructure once. Since DR-31 (Phase 3) the ledger serves only its own routes (`app/api/`, API keys in `app/auth.py`) and runs no reasoner code: a reasoner submits over HTTP or in process through `app/client.py` (`HttpClient` / `LocalClient`), and a plugin publishes only its registration and the assets its gates read. There is **no** exception for `app/main.py`: no module under `app/`, `core/`, or `eval/` imports a reasoner package or names an ATO symbol, id, template, feature, or jurisdiction (in `.py`, `.yaml`, `.sql`, or `.json`). `tests/test_framework_boundary.py` enforces this and also asserts the root pyproject declares no reasoner dependency or entry point and that each member declares its own entry points.

DecisionLedger is the framework-owned audit/governance hub. All decision bundles, replay logs, and resolution attempts live in the `decisionledger.*` Postgres schema with `reasoner_id` as the tenant column. The canonical DDL ships as package data in `app/audit/sql/`; Postgres is the framework's only infrastructure. Gates are workspace members under `gates/` that own their infrastructure (DR-28): the RAG policy gate (`gates/policy_rag`, package `policy_rag_gate`) keeps its policy chunks in its own `policy_rag` schema (DDL in its `sql/`), its ES mapping in its `es/`, and Elasticsearch in its `compose.yaml`. Each reasoner's policy corpus is loaded via `uv run python -m policy_rag_gate.corpus_loader --reasoner-id <id>` (the loader resolves the plugin's `corpus_dir`) and filtered at retrieval time.

## Tech Stack

- Python 3.11+, Pydantic v2, FastAPI, XGBoost, sentence-transformers
- Redis Streams (event queue), PostgreSQL + pgvector (vectors + audit), Elasticsearch (BM25)
- OpenAI API (GPT-4o) for the LLM policy gate
- Docker Compose V2 (`docker compose` — no hyphen, ≥ 2.20 for `include:`) for local dev; Redis comes from the ATO reasoner's `compose.yaml`
- **uv** for Python package management, venv, lockfile, and the workspace (`reasoners/*` members)
- Ruff for formatting and linting; pytest for testing; GitHub Actions for CI

## Key Architecture Decisions

See `docs/design/decisions.md` (DR-N records) for the full rationale. Load-bearing invariants:

- **Replay does NOT re-invoke the LLM.** Deterministic replay re-executes enforcement against cached intermediate states (including the logged LLM output).
- **XGBoost trains on a heuristic labeling function**, not human-labeled data. The scorer triages events into confidence bands; it doesn't detect real fraud.
- **Cross-encoder reranking has a latency-budget bypass.** If reranking exceeds the configured timeout, fall back to RRF-only results. Log which path was taken in the Decision Bundle.
- **Prompt versions are immutable.** Never modify an existing prompt YAML. Create a new version file in the reasoner's `prompts/` dir (a top-level `system:` key carries its system message). Active version is recorded in every Decision Bundle.
- **Reasoners are workspace members discovered by entry point (DR-26).** The framework never imports one; each publishes a `ReasonerPlugin` (registration incl. gate set, bypass rules, and enforcement profile; prompts/corpus dirs) and an `EvalAdapter`, and runs as its own service or in process through `LocalClient` (DR-31).
- **A reasoner records or gates (DR-29, DR-30).** A `record` submission cites a registered bypass rule and runs only mandatory gates; a `gate` submission runs mandatory then advisory gates. The final action is the most restrictive across the proposal and the gates, with one exception: on a gated decision about a novel entity, Tier 3 holds the advisory gates' verdicts for review (HOLD, or BLOCK when an advisory gate's own Tier 2 fired); mandatory gates, bypass floors, and the proposal still count. A failing gate fails closed (HOLD).

## Reasoner ↔ Framework Handoff Contract

The handoff happens at a single point: `build_submission()` in `reasoners/<domain>/src/<pkg>/assembler.py`, which returns a `core.submission.Submission`. The domain pipeline (`<pkg>/pipeline.py:run_<domain>_decision`) runs feature computation + scoring + assembly, then submits through a `LedgerClient` (`app/client.py`); its operation (`app/api/decisions.py:create_decision`, the same one `POST /api/v1/decisions` runs) calls the framework half (`app/submit.py:submit`), which validates the submission against the registry and the gates' config models, runs the decision's gate set, enforces, and persists the version-2 bundle stamped with its `registration_version`. `docs/design/reasoners/reasoner-handoff.md` still describes the pre-DR-31 Observation handoff until the Phase 4 documentation pass; DR-29 to DR-31 in `docs/design/decisions.md` are current.

Invariants to know before touching features, scorer, or policy gate:

- **A `GateContext` for every bound gate is the assembler's job, and is required on ALL submissions** — recorded ones too, which still need them for shadow evaluation.
- **`feature_set` in `ReasonerContext` must be complete** — every feature the model consumed at inference time appears here. This is what makes replay self-contained, and bypass rules are evaluated on it.
- **`ScorerOutput` lives in the reasoner package** (`ato_reasoner/scorer/output.py`), never in `core/` or `app/`. The assembler translates it into `ReasonerContext` + `GateContext` and picks the mode with `core.submission.matching_bypass_rule`.
- **Score bands are ATO's registered bypass rules** (`ato_reasoner/registry.py`, DR-30): `allow-band` (`< 0.20` → record ALLOW), `block-band` (`> 0.95` → record BLOCK); otherwise ATO asks for the policy gate. The numbers live nowhere in the framework.
- **Retrieval filter semantics:** `reasoner_id` filters both pgvector and Elasticsearch (mandatory for tenant isolation); `jurisdictions` filters both; `risk_tier` filters pgvector only.
- **Audit storage is framework-owned, multi-tenant by row.** All reasoners write to `decisionledger.decision_bundles` with `reasoner_id` as the tenant column. Reasoner-specific business keys (account_id, content_id) live inside the JSONB bundle, not as scalar columns.

## Common Commands

```bash
uv sync                        # Install framework + every workspace reasoner
docker compose up -d           # Start infra (Postgres, ES, + included reasoner compose files)
docker compose down            # Stop infra
make prepare-account_takeover  # Index ATO corpus; train scorer if missing
uv run uvicorn --factory app.main:create_app   # Run the API
make lint                      # Ruff format + lint
make typecheck                 # Pyright
make test                      # Unit tests (fast)
make test-integration          # Integration tests (requires Docker)
make test-smoke                # Smoke tests
make scenario-account_takeover # Publish 10 baseline_normal events
make eval-account_takeover     # Framework eval harness for ATO (slow); `make eval` = every reasoner, then every gate
just diagrams                  # Render D2 diagrams (requires: brew install d2)
uv run python -m ato_reasoner.generator run --scenario baseline_normal --count 50
uv run python -m policy_rag_gate.corpus_loader --reasoner-id ato-reasoner
```

`make help` lists the per-reasoner targets from each `reasoners/*/reasoner.mk`.

## Package Management

Always use `uv` — never `pip install` directly. `uv add <package>` for framework runtime deps, `uv add --dev <package>` for dev deps, `uv sync` to install/update. `uv sync` also installs the workspace reasoners (root `reasoners` dependency group, a default group). A reasoner-only dependency goes in that member: run `uv add <package>` from `reasoners/<domain>/` (never add reasoner deps to the root `[project] dependencies`). Entry points refresh only on reinstall — re-run `uv sync` after editing a member's `pyproject.toml`. The `uv.lock` lockfile is committed; do not hand-edit it.

## Writing Code

- Run `ruff check --fix` and `ruff format` on files you create or modify (the post-edit hook does this automatically).
- Type hints on all public functions; Google-style docstrings on all public classes and functions.
- For logging: structured JSON with `component`, `event_id`, `decision_id` fields; `duration_ms` for all latency measurements.
- For error handling: define explicit exception types in `core/`. Never catch bare `except:`.

## Writing Tests

Behavior-driven testing — see `docs/development/style.md` for the full philosophy. Project-specific rules:

- **`core/` contracts and enforcement routing:** test exhaustively. These are the governance guarantees.
- **LLM-touching code:** do NOT write unit tests with mocked LLM responses. The eval harness (`@pytest.mark.evaluation`) is the test suite for the policy gate.
- **Smoke tests:** tag 3–5 end-to-end tests with `@pytest.mark.smoke` (one happy path, one error path, one replay).
- **Integration tests** (event in → bundle out): mark with `@pytest.mark.integration`; highest value tests in the suite.
- Test names describe behavior: `test_enforcement_routes_schema_failure_to_HOLD`, not `test_resolve_action`.

## Scope — MVP Only

Do NOT implement: drift monitoring, canary/shadow rollout, human review queue UI, GCP deployment, custom embedding training, LLM fine-tuning, multi-tenant deployment (per-tenant isolation beyond row-level `reasoner_id`), production SLA hardening, or Kafka.

## Reference Documents

- `docs/development/style.md` — Code style and testing philosophy
- `docs/development/contributing.md` — Development workflow
- `docs/design/decisions.md` — Architecture decision records (DR-N)
- `docs/design/reasoners/reasoner-handoff.md` — Reasoner ↔ framework handoff contract and new-reasoner checklist (read before touching features, scorer, or policy gate)

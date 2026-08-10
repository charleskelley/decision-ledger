# DecisionLedger — Claude Code Instructions

## About This Project

DecisionLedger is a model-agnostic governed decision framework for AI systems. The reference implementation (ATO Reasoner) is a real-time ATO/identity risk decisioning pipeline using a hybrid ML scorer + LLM policy gate architecture with deterministic enforcement and full audit replay.

## Critical Rules

### Git

Git commits are blocked by `.claude/hooks/block-git-commit.sh` and `.claude/settings.json` permissions. Stage changes and let the user commit. No `Co-authored-by`, `Signed-off-by`, or AI-attribution trailers in commit messages — ever. Use Conventional Commits format with scopes: `core`, `ingestion`, `features`, `scorer`, `retrieval`, `policy-gate`, `enforcement`, `audit`, `eval`, `scenarios`, `infra`, `ci`.

### Code Style

See `docs/development/style.md` for the full guide. Headline rules: Ruff-only (no black/isort/flake8/pylint), 88-col line length, Pydantic v2 with `ConfigDict(strict=True, frozen=True)` for all models in `core/`, Google-style docstrings, Python 3.11+ syntax (`list[str]`, `str | None`), American English spelling everywhere.

### The `core/` Boundary — Absolute Rule

`core/` has **zero infrastructure dependencies**. No Redis, no database drivers, no LLM SDK, no FastAPI, no HTTP clients. Only: Pydantic models, enums, protocols, pure functions, and standard library imports.

If you need to add an import to `core/`, stop and reconsider. The test is: can this code be unit-tested without Docker running? If no, it belongs in `app/`, not `core/`.

Everything in `app/`, `eval/`, and `generator/` imports from `core/` and `reasoner/`. Nothing imports from `app/` except `app/` itself.

### The `reasoner/` Boundary

`reasoner/<domain>/` packages own the **full** domain implementation — pure types (events, feature vectors, scorer outputs, policy enums), ML inference and training infrastructure, ingestion adapters, the domain pipeline orchestrator, the FastAPI router, and the registration record. The only constraint is that they expose their work to the framework exclusively through the `Observation` handoff (`reasoner/<domain>/assembler.py:build_observation` → `app/decide.py:execute_decision`).

For ATO today: `reasoner/account_takeover/` holds Pydantic types (`events.py`, `features.py`, `scorer/output.py`), runtime infra (`scorer/`, `feature_service.py`, `ingestion/`), the domain pipeline (`pipeline.py`), the FastAPI router (`api.py`), settings (`settings.py`), the registration record (`registry.py`), and the query builder (`retrieval_query.py`).

`reasoner/` imports from `core/` and `app/` but `core/` never imports from `reasoner/`. Adding a second reasoner (e.g., content moderation) means creating `reasoner/content_moderation/` with the same shape — no framework changes required.

### The Framework Boundary

`app/` is framework runtime. It must not import from `reasoner.*` except in `app/main.py` (the deployment composer, which mounts each reasoner's router and supplies reasoner-specific factories like the raw-event deserializer). This rule is enforced by `tests/test_framework_boundary.py`, which also forbids ATO-coded symbols (`LoginEvent`, `AuthOutcome`, hardcoded `account_takeover.*` schema names) anywhere under `app/` outside `main.py`.

DecisionLedger is the framework-owned audit/governance hub. All decision bundles, policy chunks, replay logs, and resolution attempts live in the `decisionledger.*` Postgres schema with `reasoner_id` as the tenant column. Each reasoner's policy corpus is loaded via `python -m app.retrieval.corpus_loader --reasoner-id <id>` and filtered at retrieval time.

## Tech Stack

- Python 3.11+, Pydantic v2, FastAPI, XGBoost, sentence-transformers
- Redis Streams (event queue), PostgreSQL + pgvector (vectors + audit), Elasticsearch (BM25)
- OpenAI API (GPT-4o) for the LLM policy gate
- Docker Compose V2 (`docker compose` — no hyphen) for local dev
- **uv** for Python package management, venv, and lockfile
- Ruff for formatting and linting; pytest for testing; GitHub Actions for CI

## Key Architecture Decisions

See `docs/design/decisions.md` (DR-N records) for the full rationale. Load-bearing invariants:

- **Replay does NOT re-invoke the LLM.** Deterministic replay re-executes enforcement against cached intermediate states (including the logged LLM output).
- **XGBoost trains on a heuristic labeling function**, not human-labeled data. The scorer triages events into confidence bands; it doesn't detect real fraud.
- **Cross-encoder reranking has a latency-budget bypass.** If reranking exceeds the configured timeout, fall back to RRF-only results. Log which path was taken in the Decision Bundle.
- **Prompt versions are immutable.** Never modify an existing prompt YAML. Create a new version file. Active version is recorded in every Decision Bundle.

## Reasoner ↔ Framework Handoff Contract

The handoff happens at a single point: `build_observation()` in `reasoner/<domain>/assembler.py`. The domain pipeline (`reasoner/<domain>/pipeline.py:run_<domain>_decision`) runs feature computation + scoring + assembly, then calls the framework half (`app/decide.py:execute_decision`) which handles retrieval, the policy gate, enforcement, and audit-bundle persistence. See `docs/design/reasoners/reasoner-handoff.md` for the full contract.

Invariants to know before touching features, scorer, or policy gate:

- **`GateContext` is the assembler's job, and is required on ALL observations** — including fast-path, which still needs it for shadow evaluation.
- **`feature_set` in `ReasonerContext` must be complete** — every feature the model consumed at inference time appears here. This is what makes replay self-contained.
- **`ScorerOutput` lives in `reasoner/`**, never in `core/` or `app/`. The assembler translates it into `ReasonerContext` + `GateContext` + `FastPathRecord`.
- **Fast-path thresholds** (in assembler): `< 0.20` → FAST_PATH_ALLOW; `> 0.95` → FAST_PATH_BLOCK; otherwise ROUTE_TO_GATE (see DR-25).
- **Retrieval filter semantics:** `reasoner_id` filters both pgvector and Elasticsearch (mandatory for tenant isolation); `jurisdictions` filters both; `risk_tier` filters pgvector only.
- **Audit storage is framework-owned, multi-tenant by row.** All reasoners write to `decisionledger.decision_bundles` with `reasoner_id` as the tenant column. Reasoner-specific business keys (account_id, content_id) live inside the JSONB bundle, not as scalar columns.

## Common Commands

```bash
docker compose up -d           # Start infra
docker compose down            # Stop infra
uv sync                        # Install/update all dependencies
uv add <package>               # Add a new dependency
make lint                      # Ruff format + lint
make typecheck                 # Pyright
make test                      # Unit tests (fast)
make test-integration          # Integration tests (requires Docker)
make eval                      # Full evaluation harness (slow)
just diagrams                  # Render D2 diagrams (requires: brew install d2)
uv run python -m generator run --scenario baseline_normal --count 50
uv run python -m app.retrieval.corpus_loader
```

## Package Management

Always use `uv` — never `pip install` directly. `uv add <package>` for runtime deps, `uv add --dev <package>` for dev deps, `uv sync` to install/update. The `uv.lock` lockfile is committed; do not hand-edit it.

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

Do NOT implement: drift monitoring, canary/shadow rollout, human review queue UI, GCP deployment, custom embedding training, LLM fine-tuning, multi-tenant architecture, production SLA hardening, or Kafka.

## Reference Documents

- `docs/development/style.md` — Code style and testing philosophy
- `docs/development/contributing.md` — Development workflow
- `docs/design/decisions.md` — Architecture decision records (DR-N)
- `docs/design/reasoners/reasoner-handoff.md` — Reasoner ↔ framework handoff contract (read before touching features, scorer, or policy gate)

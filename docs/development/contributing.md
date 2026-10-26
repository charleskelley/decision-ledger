# Contributing to DecisionLedger

> DecisionLedger is currently a solo portfolio project. This document exists to establish engineering discipline from day one — and to make the project contribution-ready if it grows beyond a single author.

---

## Getting Started

### Prerequisites

- Python 3.11+ (uv will manage this if not present)
- [uv](https://docs.astral.sh/uv/) — Python package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Docker Desktop with Compose V2 (`docker compose` — no hyphen)
- Apple Silicon (M1/M2/M3) or x86_64 Linux
- An OpenAI API key (for the RAG policy gate) and, for that gate's eval judges, an Anthropic API key

### Setup

```bash
# Clone the repository
git clone https://github.com/charleskelley/decision-ledger.git
cd decision-ledger

# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Sync all dependencies (creates .venv; installs the framework plus every
# workspace reasoner, gate, and integration through the default `reasoners`,
# `gates`, and `integrations` dependency groups; add --all-groups for docs)
uv sync

# Copy environment template and add your API key
cp .env.example .env
# Edit .env to add OPENAI_API_KEY

# Start infra (the framework's Postgres, plus the RAG policy gate's Elasticsearch
# and ATO's Redis from their own compose.yaml files via include:)
docker compose up -d

# Load every reasoner's policy corpus into the RAG policy gate (ATO's
# ato-policy, the example's example-policy, the stream mock's
# mock-stream-practices, and the text mock's mock-text-regulatory and
# mock-text-practice) and train ATO's scorer if missing.
# The ledger refuses to start until every corpus a served registration binds
# is loaded.
make prepare-reasoners

# Verify everything is running
make check
```

### Running the Pipeline

```bash
# Run the ledger API (application factory; composes every installed reasoner's
# registration and every installed gate, and serves the ledger's own routes).
# Every /api/v1 route needs a key: set DECISIONLEDGER_API_KEYS first.
uv run uvicorn --factory app.main:create_app --reload     # or: make serve

# Run ATO as its own service (port 8001); without ATO_LEDGER_URL it runs the
# ledger in its own process
make serve-account_takeover

# Publish synthetic events for one ATO scenario to the reasoner's Redis stream
uv run python -m ato_reasoner.generator run --scenario baseline_normal --count 50
make scenario-account_takeover          # 10 baseline_normal events

# Run the eval harnesses: the framework's for one reasoner, the RAG policy
# gate's on one reasoner's datasets, or every reasoner's and every gate's
make eval-account_takeover
make eval-gate-policy_rag REASONER=ato-reasoner
make eval

# Replay a specific decision (API running; the reasoner's or the operator's key)
curl -s -X POST localhost:8000/api/v1/decisions/<decision_id>/replay \
  -H 'X-DecisionLedger-Key: <key>'
# Verify the evidence it relied on (escrow and each gate's check; ?live=true
# also checks each source as it is now)
curl -s -X POST localhost:8000/api/v1/decisions/<decision_id>/verify \
  -H 'X-DecisionLedger-Key: <key>'
```

Per-reasoner targets come from each `reasoners/<name>/reasoner.mk` and per-gate targets from each `gates/<name>/gate.mk`; `make help` lists them. The [local runbook](runbook-local.md) walks through the whole API.

> **Note:** `make` targets use `uv run` internally. If running Python commands
> directly, prefix with `uv run` to ensure the correct virtualenv and
> dependencies are used. Alternatively, activate the venv with
> `source .venv/bin/activate`.

---

## Development Workflow

### Before You Start

1. Read `docs/development/style.md` — it covers formatting, naming, Pydantic conventions, commit messages, and all the style guides we follow.
2. Read `docs/design/decisions.md` — it explains *why* the system is designed the way it is.
3. Understand the `core/` boundary — `core/` has no infrastructure dependencies. If your change needs to import a database driver or FastAPI, it belongs in `app/`, not `core/`; an LLM SDK belongs only in the RAG policy gate's `llm/` adapters (DR-23). If it is specific to one domain (its events, features, model, Redis, prompts, corpus), it belongs in that reasoner's workspace member under `reasoners/`; if it is how one gate reaches its verdict (retrieval, a model client, its schema), it belongs in that gate's member under `gates/`. Neither belongs in the framework.

### Making Changes

1. **Create a branch** from `main`:
   ```bash
   git checkout -b feat/your-feature-name
   ```
   Branch naming: `<type>/<short-description>` — e.g., `feat/geo-impossible-detector`, `fix/replay-hash-mismatch`.

2. **Write code** following `docs/development/style.md`.

3. **Run checks locally** before pushing:
   ```bash
   make lint      # Ruff format + lint
   make typecheck # Pyright
   make test      # pytest
   make eval      # Full evaluation harness (slow — run before PR, not every commit)
   ```

4. **Commit** using [Conventional Commits](https://www.conventionalcommits.org/):
   ```
   feat(scorer): add SHAP value extraction to scorer output
   ```

5. **Push and open a PR** against `main`. CI runs lint, typecheck, and unit tests on every push to `main` and every PR to `main`, and integration/smoke against service containers on pushes to `main` and same-repository PRs. The eval harnesses run only on demand via `workflow_dispatch` — invoke them manually before a release candidate.

### What Makes a Good PR

- **Focused scope.** One logical change per PR. "Add scenario generator + build
  policy corpus + wire up ingestion" is three PRs, not one.
- **Tests included.** If you change pipeline behavior, include or update tests.
  If you add a new eval dimension, include the golden set entries.
- **Under ~400 lines of non-test code.** Larger PRs are harder to review and
  more likely to introduce subtle issues. If your change is genuinely large,
  break it into stacked PRs.
- **Clear description.** What does this change? Why? Any tradeoffs or known
  limitations?

### Prompt Version Changes

Prompt changes are treated as code changes with extra scrutiny:

1. Create a new prompt version file in the reasoner's `prompts/` directory (ATO: `reasoners/account_takeover/src/ato_reasoner/prompts/ato-v{n+1}.yaml`), carrying its own `system:` text if the reasoner needs domain framing
2. Do **not** modify the existing version file — old versions are immutable for
   audit trail integrity.
3. Point the reasoner's assembler at the new `template_id`. There is no registration edit: the RAG policy gate authorizes every template the reasoner ships in its `prompts/` directory (DR-28).
4. Run both eval harnesses against the new prompt version: the framework's (`make eval-<reasoner>`) and the policy gate's retrieval, faithfulness, and citation dimensions on that reasoner's datasets (`make eval-gate-policy_rag REASONER=<reasoner_id>`); `make eval` runs both for every reasoner.
5. Include the eval results in the PR description. The eval workflow is manual, so this run is the gate.

---

## Project Structure

```
decision-ledger/
├── core/                    # Framework contracts — no infrastructure dependencies
│   ├── submission.py        # Submission — the wire contract a reasoner hands the ledger
│   ├── bundle.py            # DecisionBundle (schema version 2), GateRecord
│   ├── identity.py          # NativeSubject, ResolvedIdentity, the entity journal's types
│   ├── observation/         # ReasonerContext, GateContext, registration (GateBinding, BypassRule)
│   ├── gate/                # Universal gate contracts (GateOutput, GateVerdict, EvidenceRef, predicates, combinator)
│   ├── resolution/          # Resolution attempt log
│   ├── llm/                 # LLMClient protocol
│   └── eval/                # Eval metric contracts
├── app/                     # Framework runtime — imports from core/, never from a reasoner or gate
│   ├── main.py, plugin.py, plugins.py, bootstrap.py   # create_app factory + plugin seam
│   ├── api/                 # The ledger's HTTP routes and operations (/api/v1), problem documents
│   ├── auth.py, client.py   # API keys → principals; LocalClient / HttpClient
│   ├── submit.py            # decide / submit: validation, identity, gate set, enforcement, bundle
│   ├── gates.py             # GatePlugin / Gate / ConfigAuthorizer seam, gate discovery
│   ├── identity/            # The identity step: resolver discovery and the passthrough
│   ├── enforcement/         # Deterministic rule-based action resolution
│   ├── audit/               # Bundle store, registrations, journals, canonical DDL (sql/)
│   └── monitoring/          # Structured logging, latency tracking
├── eval/                    # Reasoner-agnostic evaluation harness (consistency, robustness, shadow; EvalAdapter)
├── gates/                   # One uv workspace member per gate
│   ├── rules/               # Zero-infrastructure rules gate (rules_gate)
│   └── policy_rag/          # RAG policy gate (policy_rag_gate): retrieval, prompts, LLM adapters, own schema, eval, gate.mk, compose.yaml
├── reasoners/               # One uv workspace member per reasoner
│   ├── account_takeover/    # pyproject.toml, reasoner.mk, compose.yaml, src/ato_reasoner/, tests/
│   ├── example/             # The minimal example reasoner (widgets), src/example_reasoner/
│   ├── mock_stream/         # mock-stream-reasoner: the real-time event scorer pattern (DR-35)
│   ├── mock_batch/          # mock-batch-reasoner: the daily batch scorer pattern (DR-35)
│   ├── mock_text/           # mock-text-reasoner: the LLM-native classifier pattern (DR-35)
│   └── mock_daily/          # mock-daily-reasoner: the deterministic computation with exceptions pattern (DR-35)
├── integrations/mcp/        # The MCP server (decisionledger-mcp), a client of the ledger
├── schemas/                 # submission-v2.json, bundle-v2.json (exported by make schemas)
├── conformance/             # Black-box HTTP contract suite (make conformance)
├── tests/                   # Framework tests (+ _stub_reasoner/, _fake_gate/, _stub_resolver/: test-only doubles)
├── docs/                    # MkDocs site: design docs, decision records, D2 diagrams
└── README.md                # Project overview, quickstart, architecture diagram
```

The style guide is [`docs/development/style.md`](style.md) and the decision records are [`docs/design/decisions.md`](../design/decisions.md).

### The `core/` Boundary — The Most Important Rule

`core/` is a pure Python library with zero infrastructure dependencies:

- ✅ Pydantic models, enums, dataclasses, protocols, pure functions
- ✅ `import json`, `import hashlib`, `import enum`, `from pydantic import BaseModel`
- ❌ `import redis`, `import psycopg`, `import openai`, `import fastapi`
- ❌ Any network calls, file I/O to external systems, or database queries

Everything else (`app/`, `eval/`, every reasoner under `reasoners/`, every gate under `gates/`) imports from `core/`. `core/`, `app/`, and `eval/` never import from a reasoner or a gate; both are discovered through entry points, and `tests/test_framework_boundary.py` fails the build on any leak.

**If you're unsure whether something belongs in `core/` or `app/`:** ask whether it can be unit-tested without Docker running. If yes → `core/`. If no → `app/`. If it names a domain concept → the reasoner.

---

## Testing

### Test Organization

Framework tests mirror the framework tree and run against the test-only doubles (a stub reasoner, fake gates, a stub identity resolver), never ATO or a real gate; each member's tests live in that member:

```
tests/
├── _stub_reasoner/                 # minimal reasoner used only by framework tests
├── _fake_gate/                     # fake gates used only by framework tests
├── _stub_resolver/                 # stub identity resolver used only by framework tests
├── core/
│   ├── observation/test_registration.py
│   └── test_bundle.py
├── app/
│   ├── test_enforcement.py
│   └── test_plugin_discovery.py
├── eval/
│   └── test_consistency_dimension.py
├── integration/
│   └── test_bundle_store.py
├── test_framework_boundary.py
└── test_schemas.py                 # fails when schemas/*.json drift from the models
reasoners/account_takeover/tests/   # ATO unit, integration, generator, and eval-driver tests
reasoners/mock_*/tests/             # the mocks' tests: Docker-free, over the in-memory ledger, except the stream mock's one integration acceptance test
gates/policy_rag/tests/             # RAG gate unit and integration tests (eval/: its harness's unit tests)
conformance/                        # the HTTP contract suite (marker `conformance`; make conformance)
```

### Test Categories

- **Unit tests** (`tests/core/`, `tests/app/`, `tests/eval/`, and the member's unit tests): Pure logic, no infrastructure. Fast. Run on every commit.
- **Smoke tests**: Minimal end-to-end sanity check — one happy path, one error path, one replay through the full pipeline. Subset of integration, but fast enough to run during development for quick confidence. Mark with `@pytest.mark.smoke`.
- **Integration tests** (`tests/integration/`, `reasoners/<name>/tests/integration/`, `gates/<name>/tests/integration/`): Require Docker services. Run in CI and before PRs. Mark with `@pytest.mark.integration`.
- **Evaluation tests**: any test that calls a live model. Slow, makes LLM calls, costs money. Mark with `@pytest.mark.evaluation`: `make test` excludes it and `make test-all` runs it. No test carries the mark today: the eval harnesses themselves (`make eval-<reasoner>`, `make eval-gate-policy_rag`) are the LLM test suite, and `tests/eval/` and `gates/policy_rag/tests/eval/` hold the harnesses' unit tests, which stub every model.

### Running Tests

```bash
make test                    # Unit tests only (fast)
make test-smoke              # Smoke tests (quick end-to-end sanity check, requires Docker)
make test-integration        # Integration tests (requires Docker)
make eval                    # Every reasoner's eval, then every gate's (slow, costs money)
make test-all                # Everything
make conformance             # HTTP contract suite against a running ledger (DECISIONLEDGER_URL + keys)
```

### Writing Tests

- Use `pytest` exclusively. No unittest.TestCase.
- Use fixtures for shared setup. Prefer factory fixtures over complex setup methods.
- Test names describe the behavior: `test_enforcement_routes_schema_failure_to_HOLD`, not `test_enforcement_1`.
- For Pydantic models, test both valid construction and expected validation errors.
- For the enforcement layer, test every routing trigger explicitly.

---

## Evaluation Harness

The eval harness is a first-class component, not an afterthought. Changes that affect decision quality must include eval results.

### Five Dimensions

Two harnesses run them. The framework's (`eval/`, `make eval-<reasoner>`) drives each reasoner's own pipeline through its `EvalAdapter`; the RAG policy gate's (`policy_rag_gate.eval`, `make eval-gate-policy_rag`) runs on the datasets each reasoner ships for that gate.

| # | Dimension | Harness | What It Tests | CI Gate |
|---|-----------|---------|---------------|---------|
| 01 | Retrieval Quality | RAG policy gate | Right policy evidence retrieved | Precision@5 ≥0.40, Recall@5 ≥0.75, MRR ≥0.70 |
| 02 | Generation Faithfulness | RAG policy gate | Rationale grounded in retrieved evidence | RAGAS ≥0.65 (MVP), Hallucination = 0 |
| 03 | Decision Consistency | Framework | Same input → same action across orderings | Action Stability = 1.0 |
| 04 | Citation Accuracy | RAG policy gate | Citations actually support claims | Relevance ≥0.80 |
| 05 | Adversarial Robustness | Framework | Correct behavior under attack | Injection Resistance = 1.0 |

The framework's harness also runs the **shadow** dimension (DR-30): it re-decides a sample of the run's recorded decisions with the reasoner's advisory gates and reports agreement. It is report-only until a baseline calibrates a threshold.

### Golden Sets

Golden sets belong to the reasoner they evaluate and ship with its eval adapter; the retrieval, faithfulness, and citation sets are the ones the RAG policy gate's harness reads. For ATO they live under `reasoners/account_takeover/src/ato_reasoner/eval/datasets/`:

- `retrieval/golden_queries.yaml` — 12 curated retrieval queries with annotated relevant policy chunks
- `scenarios/*.yaml` — 4 scenarios × 3 orderings with expected actions
- `faithfulness/golden_outputs.yaml` — reference gate outputs with retrieved-snippet ground truth
- `citations/golden_outputs.yaml` — reviewed citations rated for claim support
- `robustness/{injection,malformed,novel,fallback}.yaml` — adversarial event variants with expected handling

Golden set changes are high-impact and require careful review.

---

## CI Pipeline

GitHub Actions runs four workflows:

| Workflow             | Trigger                            | What it runs                                                                     |
|----------------------|------------------------------------|----------------------------------------------------------------------------------|
| `ci.yaml`            | Push to `main`, PR to `main`       | Lint (Ruff + sqlfluff) → typecheck (pyright) → unit tests → DR-23 boundary check |
| `integration.yaml`   | Push to `main`, same-repository PR to `main` | Per (reasoner, gate) pair (`matrix.reasoner`, `matrix.gate`): `make prepare-gate-<gate>`, `make prepare-<reasoner>`, then `make test-integration` + `make test-smoke` against service containers |
| `eval.yaml`          | `workflow_dispatch` only           | Per (reasoner, gate) pair: prepare both, then `make eval-<reasoner>` (framework harness) and `make eval-gate-<gate>` on that reasoner's datasets; uploads the reports as a workflow artifact |
| `docs.yaml`          | Push or PR to `main` touching the docs, `workflow_dispatch` | `mkdocs build --strict`; deploys to GitHub Pages on push to `main` |

- **Gate check.** The eval harness compares each dimension's score against its
  threshold. Any regression below threshold fails the workflow.
- **Replay tests** (part of the integration suite): write decisions, replay
  them from the stored bundles, and require `matched`: the decision action,
  every gate's enforced action, and a record's bypass-rule checks are
  reproduced.
- **Pre-push hook.** `make install-hooks` wires `make verify-push` as a
  pre-push hook, so locally validated state matches what CI sees.

### Quality and Integration Must Pass Before Merge

No exceptions on `ci.yaml` and `integration.yaml`. The eval workflow has no
schedule — drive it manually via `workflow_dispatch` to validate a release
candidate before promoting a new baseline.

---

## Infrastructure

### Local Development

All development runs locally via Docker Compose. No cloud dependency during iteration.

```bash
docker compose up -d          # Start Postgres (pgvector image), plus the RAG gate's Elasticsearch and ATO's Redis (included)
docker compose down           # Stop everything
docker compose down -v        # Stop and remove volumes (full reset)
```

### Cloud Deployment (Stretch Goal)

Cloud deployment (AWS: ECS + ElastiCache + RDS + OpenSearch) is a planned stretch goal; no Terraform is in the repository yet. See [Infrastructure](../design/infrastructure.md) for the target layout.

- Never commit `.tfstate` files or `.tfvars` with secrets.
- All Terraform changes require `terraform plan` output in the PR description.

---

## Secrets & Security

- **Never commit secrets.** API keys, database passwords, Terraform state — none of these belong in git.
- `.env` is gitignored. Use `.env.example` as a committed template with placeholder values.
- Two external secrets are used during MVP: `OPENAI_API_KEY` (the RAG policy gate) and `ANTHROPIC_API_KEY` (its eval judges). The ledger's own API keys (`DECISIONLEDGER_API_KEYS`, `DECISIONLEDGER_OPERATOR_KEY`) are secrets too. Pass them via environment variables; see [Secrets](../operations/secrets.md).
- If you discover a committed secret, rotate it immediately and force-push to remove it from history.

---

## Questions & Decisions

If you encounter a design question that isn't covered by existing code or documentation:

1. Check `docs/design/decisions.md` — the answer may already be documented.
2. Check `docs/development/style.md` — it may be a style convention question.
3. If neither covers it, make a judgment call, document it, and move on. A consistent decision made now is better than a perfect decision made later. If it's a significant architectural choice, add a decision record.

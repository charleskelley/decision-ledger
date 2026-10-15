# Contributing to DecisionLedger

> DecisionLedger is currently a solo portfolio project. This document exists to establish engineering discipline from day one — and to make the project contribution-ready if it grows beyond a single author.

---

## Getting Started

### Prerequisites

- Python 3.11+ (uv will manage this if not present)
- [uv](https://docs.astral.sh/uv/) — Python package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Docker Desktop with Compose V2 (`docker compose` — no hyphen)
- Apple Silicon (M1/M2/M3) or x86_64 Linux
- An OpenAI API key (for the LLM policy gate)

### Setup

```bash
# Clone the repository
git clone https://github.com/charleskelley/decision-ledger.git
cd decision-ledger

# Install uv if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# Sync all dependencies (creates .venv; installs the framework plus every
# reasoner workspace member through the default `reasoners` dependency group)
uv sync

# Copy environment template and add your API key
cp .env.example .env
# Edit .env to add OPENAI_API_KEY

# Start infra (Postgres + Elasticsearch, plus each reasoner's compose.yaml via include:)
docker compose up -d

# Index the reference reasoner's corpus and train its scorer if missing
make prepare-account_takeover

# Verify everything is running
make check
```

### Running the Pipeline

```bash
# Run the API (application factory; mounts every discovered reasoner plugin)
uv run uvicorn --factory app.main:create_app --reload

# Publish synthetic events for one ATO scenario to the reasoner's Redis stream
uv run python -m ato_reasoner.generator run --scenario baseline_normal --count 50
make scenario-account_takeover          # 10 baseline_normal events

# Run the eval harness (one reasoner, or every installed reasoner)
make eval-account_takeover
make eval

# Replay a specific decision (API running)
curl -s -X POST localhost:8000/api/v1/decisions/<decision_id>/replay
```

Per-reasoner targets come from each `reasoners/<name>/reasoner.mk`; `make help` lists them.

> **Note:** `make` targets use `uv run` internally. If running Python commands
> directly, prefix with `uv run` to ensure the correct virtualenv and
> dependencies are used. Alternatively, activate the venv with
> `source .venv/bin/activate`.

---

## Development Workflow

### Before You Start

1. Read `docs/development/style.md` — it covers formatting, naming, Pydantic conventions, commit messages, and all the style guides we follow.
2. Read `docs/design/decisions.md` — it explains *why* the system is designed the way it is.
3. Understand the `core/` boundary — `core/` has no infrastructure dependencies. If your change needs to import a database driver, an LLM SDK, or FastAPI, it belongs in `app/`, not `core/`. If it is specific to one domain (its events, features, model, Redis, prompts, corpus), it belongs in that reasoner's workspace member under `reasoners/`, not in the framework.

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

5. **Push and open a PR** against `main`. CI runs lint, typecheck, unit tests, and integration/smoke against service containers on every push and PR. The 5D eval harness runs only on demand via `workflow_dispatch` — invoke it manually before a release candidate.

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
│   ├── observation/         # ReasonerContext, GateContext, registration (GateBinding, BypassRule)
│   ├── gate/                # Universal gate contracts (GateOutput, GateVerdict, EvidenceRef, predicates, combinator)
│   ├── resolution/          # Resolution attempt log
│   ├── llm/                 # LLMClient protocol
│   └── eval/                # Eval metric contracts
├── app/                     # Framework runtime — imports from core/, never from a reasoner
│   ├── main.py, plugin.py, plugins.py, bootstrap.py   # create_app factory + plugin seam
│   ├── submit.py            # submit (framework half of the handoff: gate set, enforcement, bundle)
│   ├── gates.py             # GatePlugin / Gate / ConfigAuthorizer seam, gate discovery
│   ├── enforcement/         # Deterministic rule-based action resolution
│   ├── audit/               # Decision Bundle construction + replay, canonical DDL (sql/)
│   └── monitoring/          # Structured logging, latency tracking
├── eval/                    # Reasoner-agnostic evaluation harness (consistency, robustness; EvalAdapter)
├── gates/                   # One uv workspace member per gate
│   ├── rules/               # Zero-infrastructure rules gate (rules_gate)
│   └── policy_rag/          # RAG policy gate (policy_rag_gate): retrieval, prompts, LLM adapters, own schema, eval, gate.mk, compose.yaml
├── reasoners/               # One uv workspace member per reasoner
│   └── account_takeover/    # pyproject.toml, reasoner.mk, compose.yaml, src/ato_reasoner/, tests/
├── tests/                   # Framework tests (+ _stub_reasoner/, a test-only second reasoner)
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

Everything else (`app/`, `eval/`, every reasoner under `reasoners/`) imports from `core/`. `core/`, `app/`, and `eval/` never import from a reasoner; reasoners are discovered through entry points, and `tests/test_framework_boundary.py` fails the build on any leak.

**If you're unsure whether something belongs in `core/` or `app/`:** ask whether it can be unit-tested without Docker running. If yes → `core/`. If no → `app/`. If it names a domain concept → the reasoner.

---

## Testing

### Test Organization

Framework tests mirror the framework tree and run against the test-only stub reasoner, never ATO; each reasoner's tests live in its own member:

```
tests/
├── _stub_reasoner/                 # minimal second reasoner used only by framework tests
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
└── test_framework_boundary.py
reasoners/account_takeover/tests/   # ATO unit, integration, generator, and eval-driver tests
```

### Test Categories

- **Unit tests** (`tests/core/`, `tests/app/`, `tests/eval/`, and the member's unit tests): Pure logic, no infrastructure. Fast. Run on every commit.
- **Smoke tests**: Minimal end-to-end sanity check — one happy path, one error path, one replay through the full pipeline. Subset of integration, but fast enough to run during development for quick confidence. Mark with `@pytest.mark.smoke`.
- **Integration tests** (`tests/integration/`, `reasoners/<name>/tests/integration/`): Require Docker services. Run in CI and before PRs. Mark with `@pytest.mark.integration`.
- **Evaluation tests** (`tests/eval/` and `eval/`): Full pipeline evaluation. Slow, makes LLM calls, costs money. Run before PRs and in CI release gates. Mark with `@pytest.mark.evaluation`.

### Running Tests

```bash
make test                    # Unit tests only (fast)
make test-smoke              # Smoke tests (quick end-to-end sanity check, requires Docker)
make test-integration        # Integration tests (requires Docker)
make eval                    # Full evaluation harness (slow, costs money)
make test-all                # Everything
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

| # | Dimension | What It Tests | CI Gate |
|---|-----------|---------------|---------|
| 01 | Retrieval Quality | Right policy evidence retrieved | Precision ≥0.80, Recall ≥0.75 |
| 02 | Generation Faithfulness | Rationale grounded in retrieved evidence | RAGAS ≥0.65 (MVP), Hallucination = 0 |
| 03 | Decision Consistency | Same input → same action across orderings | Action Stability = 1.0 |
| 04 | Citation Accuracy | Citations actually support claims | Relevance ≥0.80 |
| 05 | Adversarial Robustness | Correct behavior under attack | Injection Resistance = 1.0 |

### Golden Sets

Golden sets belong to the reasoner they evaluate and ship with its eval adapter. For ATO they live under `reasoners/account_takeover/src/ato_reasoner/eval/datasets/`:

- `retrieval/golden_queries.yaml` — 12 curated retrieval queries with annotated relevant policy chunks
- `scenarios/*.yaml` — 4 scenarios × 3 orderings with expected actions
- `faithfulness/golden_outputs.yaml` — reference gate outputs with retrieved-snippet ground truth
- `citations/golden_outputs.yaml` — reviewed citations rated for claim support
- `robustness/{injection,malformed,novel,fallback}.yaml` — adversarial event variants with expected handling

Golden set changes are high-impact and require careful review.

---

## CI Pipeline

GitHub Actions runs three workflows:

| Workflow             | Trigger                            | What it runs                                                                     |
|----------------------|------------------------------------|----------------------------------------------------------------------------------|
| `ci.yaml`            | Push to `main`, PR to `main`       | Lint (Ruff + sqlfluff) → typecheck (pyright) → unit tests → DR-23 boundary check |
| `integration.yaml`   | Push to `main`, PR to `main`       | Per (reasoner, gate) pair (`matrix.reasoner`, `matrix.gate`): `make prepare-gate-<gate>`, `make prepare-<reasoner>`, then `make test-integration` + `make test-smoke` against service containers |
| `eval.yaml`          | `workflow_dispatch` only           | Per (reasoner, gate) pair: prepare both, then `make eval-<reasoner>` (framework harness) and `make eval-gate-<gate>` on that reasoner's datasets; uploads the reports as a workflow artifact |

- **Gate check.** The eval harness compares each dimension's score against its
  threshold. Any regression below threshold fails the workflow.
- **Replay test** (part of integration tests): loads N Decision Bundles and
  replays them. Output must be byte-identical to the original.
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
docker compose up -d          # Start PostgreSQL+pgvector, Elasticsearch, and ATO's Redis (included)
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
- Two external secrets are used during MVP: `OPENAI_API_KEY` (policy gate) and `ANTHROPIC_API_KEY` (eval judges). Pass them via environment variables; see [Secrets](../operations/secrets.md).
- If you discover a committed secret, rotate it immediately and force-push to remove it from history.

---

## Questions & Decisions

If you encounter a design question that isn't covered by existing code or documentation:

1. Check `docs/design/decisions.md` — the answer may already be documented.
2. Check `docs/development/style.md` — it may be a style convention question.
3. If neither covers it, make a judgment call, document it, and move on. A consistent decision made now is better than a perfect decision made later. If it's a significant architectural choice, add a decision record.

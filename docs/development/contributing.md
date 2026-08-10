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

# Sync all dependencies (creates .venv, installs everything, lockfile)
uv sync

# Copy environment template and add your API key
cp .env.example .env
# Edit .env to add OPENAI_API_KEY

# Start infra
docker compose up -d

# Verify everything is running
make check
```

### Running the Pipeline

```bash
# Generate events for a scenario
uv run python -m scenarios run --scenario baseline_normal --count 50

# Run the full decision path
make scenario

# Run the eval harness
make eval

# Replay a specific decision
uv run python -m decision_ledger.audit replay --id <bundle_id>
```

> **Note:** `make` targets use `uv run` internally. If running Python commands
> directly, prefix with `uv run` to ensure the correct virtualenv and
> dependencies are used. Alternatively, activate the venv with
> `source .venv/bin/activate`.

---

## Development Workflow

### Before You Start

1. Read `STYLE.md` — it covers formatting, naming, Pydantic conventions, commit messages, and all the style guides we follow.
2. Read `DECISIONS.md` — it explains *why* the system is designed the way it is.
3. Understand the `core/` boundary — `core/` has no infrastructure dependencies. If your change needs to import Redis, a database driver, an LLM SDK, or FastAPI, it belongs in `app/`, not `core/`.

### Making Changes

1. **Create a branch** from `main`:
   ```bash
   git checkout -b feat/your-feature-name
   ```
   Branch naming: `<type>/<short-description>` — e.g., `feat/geo-impossible-detector`, `fix/replay-hash-mismatch`.

2. **Write code** following `STYLE.md`.

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

5. **Push and open a PR** against `main`. CI runs lint, typecheck, unit tests, and integration/smoke against service containers on every push and PR. The 5D eval harness runs nightly and on demand via `workflow_dispatch` — invoke it manually before a release candidate.

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

1. Create a new prompt version file: `app/policy_gate/prompts/v{n+1}.yaml`
2. Do **not** modify the existing version file — old versions are immutable for
   audit trail integrity.
3. Run the full eval harness against the new prompt version.
4. Include the eval results in the PR description.
5. CI will block the merge if the eval gate fails.

---

## Project Structure

```
decision-ledger/
├── core/                    # Pure business logic — no infrastructure dependencies
│   ├── decision/            # Decision schema, action space, bundle contracts
│   ├── policy/              # Policy gate contracts, citation schema, enforcement rules
│   └── eval/          # Eval dimension contracts, metric interfaces
├── app/                     # Runtime pipeline — imports from core/
│   ├── ingestion/           # Redis Streams consumer, dedup, bounded lateness
│   ├── features/            # Sliding window feature computation
│   ├── scorer/              # XGBoost fast scorer
│   ├── retrieval/           # Hybrid RAG (pgvector + Elasticsearch + reranker)
│   ├── policy_gate/         # LLM reasoning layer
│   │   └── prompts/         # Versioned YAML prompt templates
│   ├── enforcement/         # Deterministic rule-based action resolution
│   ├── audit/               # Decision Bundle construction + replay
│   └── monitoring/          # Structured logging, latency tracking
├── eval/                    # Evaluation harness (5 dimensions)
├── generator/           # Synthetic event generator (8 named patterns)
├── tests/                   # Mirrors source tree
├── infra/                   # Terraform (AWS primary)
├── docs/                    # PlantUML C4 diagrams, architecture docs
├── STYLE.md                 # Code style guide and conventions
├── CONTRIBUTING.md          # This file
├── DECISIONS.md             # Architecture decision records (D1–D10)
└── README.md                # Project overview, quickstart, architecture diagram
```

### The `core/` Boundary — The Most Important Rule

`core/` is a pure Python library with zero infrastructure dependencies:

- ✅ Pydantic models, enums, dataclasses, protocols, pure functions
- ✅ `import json`, `import hashlib`, `import enum`, `from pydantic import BaseModel`
- ❌ `import redis`, `import asyncpg`, `import openai`, `import fastapi`
- ❌ Any network calls, file I/O to external systems, or database queries

Everything else (`app/`, `eval/`, `generator/`) imports from `core/`. Nothing imports from `app/` except `app/` itself.

**If you're unsure whether something belongs in `core/` or `app/`:** ask whether it can be unit-tested without Docker running. If yes → `core/`. If no → `app/`.

---

## Testing

### Test Organization

Tests mirror the source tree:

```
tests/
├── core/
│   ├── decision/
│   │   └── test_bundle.py
│   └── policy/
│       └── test_enforcement_rules.py
├── app/
│   ├── ingestion/
│   │   └── test_dedup.py
│   └── scorer/
│       └── test_risk_score.py
└── eval/
    └── test_consistency.py
```

### Test Categories

- **Unit tests** (`tests/core/`): Pure logic, no infrastructure. Fast. Run on every commit.
- **Smoke tests**: Minimal end-to-end sanity check — one happy path, one error path, one replay through the full pipeline. Subset of integration, but fast enough to run during development for quick confidence. Mark with `@pytest.mark.smoke`.
- **Integration tests** (`tests/app/`): Require Docker services. Run in CI and before PRs. Mark with `@pytest.mark.integration`.
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

- `eval/datasets/retrieval/golden_queries.yaml` — 12 curated retrieval queries with annotated relevant policy chunks
- `eval/datasets/scenarios/*.yaml` — 4 scenarios × 3 orderings with expected actions
- `eval/datasets/faithfulness/golden_outputs.yaml` — reference gate outputs with retrieved-snippet ground truth
- `eval/datasets/citations/golden_outputs.yaml` — reviewed citations rated for claim support
- `eval/datasets/robustness/{injection,malformed,novel,fallback}.yaml` — adversarial event variants with expected handling

Golden set changes are high-impact and require careful review.

---

## CI Pipeline

GitHub Actions runs three workflows:

| Workflow             | Trigger                            | What it runs                                                                     |
|----------------------|------------------------------------|----------------------------------------------------------------------------------|
| `ci.yaml`            | Push to `main`, PR to `main`       | Lint (Ruff + sqlfluff) → typecheck (pyright) → unit tests → DR-23 boundary check |
| `integration.yaml`   | Push to `main`, PR to `main`       | `make test-integration` + `make test-smoke` against service containers           |
| `eval.yaml`          | `workflow_dispatch` + nightly cron | `make eval` — full 5D harness; uploads the report as a workflow artifact         |

- **Gate check.** The eval harness compares each dimension's score against its
  threshold. Any regression below threshold fails the workflow.
- **Replay test** (part of integration tests): loads N Decision Bundles and
  replays them. Output must be byte-identical to the original.
- **Pre-push hook.** `make install-hooks` wires `make verify-push` as a
  pre-push hook, so locally validated state matches what CI sees.

### Quality and Integration Must Pass Before Merge

No exceptions on `ci.yaml` and `integration.yaml`. The eval workflow is
nightly — drive it manually via `workflow_dispatch` to validate a release
candidate before promoting a new baseline.

---

## Infrastructure

### Local Development

All development runs locally via Docker Compose. No cloud dependency during iteration.

```bash
docker compose up -d          # Start Redis, PostgreSQL+pgvector, Elasticsearch
docker compose down           # Stop everything
docker compose down -v        # Stop and remove volumes (full reset)
```

### Cloud Deployment (Stretch Goal)

Terraform modules in `infra/` target AWS (ECS + ElastiCache + RDS + OpenSearch). Deployment is validated but not required for MVP.

- Never commit `.tfstate` files or `.tfvars` with secrets.
- Use `infra/environments/dev/terraform.tfvars.example` as a template.
- All Terraform changes require `terraform plan` output in the PR description.

---

## Secrets & Security

- **Never commit secrets.** API keys, database passwords, Terraform state — none of these belong in git.
- `.env` is gitignored. Use `.env.example` as a committed template with placeholder values.
- The OpenAI API key is the only external secret required during MVP. Pass it via environment variable `OPENAI_API_KEY`.
- If you discover a committed secret, rotate it immediately and force-push to remove it from history.

---

## Questions & Decisions

If you encounter a design question that isn't covered by existing code or documentation:

1. Check `DECISIONS.md` — the answer may already be documented.
2. Check `STYLE.md` — it may be a style convention question.
3. If neither covers it, make a judgment call, document it, and move on. A consistent decision made now is better than a perfect decision made later. If it's a significant architectural choice, add a DECISIONS.md entry.

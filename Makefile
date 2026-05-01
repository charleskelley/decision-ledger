# =============================================================================
# DecisionLedger Makefile
# All Python commands run via `uv run` — never call python or pytest directly.
# =============================================================================

.DEFAULT_GOAL := help

.PHONY: help lint lint-sql typecheck test test-smoke test-integration test-replay \
        eval test-all check scenario build-policy-index install-hooks \
        train eval-model

# -----------------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------------

help:
	@echo ""
	@echo "  DecisionLedger — available targets"
	@echo ""
	@echo "  Quality"
	@echo "    lint              Ruff format + lint (with autofix)"
	@echo "    lint-sql          SQLFluff lint all .sql files (postgres dialect)"
	@echo "    typecheck         Pyright static type checking"
	@echo "    check             lint + lint-sql + typecheck + test (full local gate)"
	@echo ""
	@echo "  Testing"
	@echo "    test              Unit tests (no Docker required)"
	@echo "    test-smoke        Quick E2E sanity check (3–5 tests)"
	@echo "    test-integration  Integration tests (requires Docker)"
	@echo "    test-replay       Deterministic replay check (20 random bundles)"
	@echo "    test-all          All test suites"
	@echo ""
	@echo "  Evaluation"
	@echo "    eval              Full 5-dimension evaluation harness (slow, costs money)"
	@echo ""
	@echo "  Scorer"
	@echo "    train             Train the ATO XGBoost scorer (writes artifact + card + CSVs)"
	@echo "    eval-model        Evaluate the trained scorer on a fresh-seed test set"
	@echo ""
	@echo "  Development"
	@echo "    scenario          Generate 10 baseline_normal events (quick smoke)"
	@echo "    build-policy-index  Embed and index the policy corpus"
	@echo "    install-hooks     Install pre-commit git hooks"
	@echo ""

# -----------------------------------------------------------------------------
# Quality
# -----------------------------------------------------------------------------

lint:
	uv run ruff check --fix .
	uv run ruff format .

lint-sql:
	uv run sqlfluff lint --dialect postgres infra/

typecheck:
	uv run pyright

check: lint lint-sql typecheck test

# -----------------------------------------------------------------------------
# Testing
# -----------------------------------------------------------------------------

test:
	uv run pytest tests/ -m "not integration and not evaluation and not slow and not smoke" -v

test-smoke:
	uv run pytest tests/ -m smoke -v

test-integration:
	uv run pytest tests/ -m integration -v

test-replay:
	uv run pytest tests/ -m replay -v

eval:
	uv run pytest eval/ -m evaluation -v

# -----------------------------------------------------------------------------
# Scorer training and evaluation
# -----------------------------------------------------------------------------

train:
	uv run python -m app.scorer train \
		--output app/scorer/models/ato-v1.ubj \
		--samples 5000

eval-model:
	uv run python -m app.scorer eval --model app/scorer/models/ato-v1.ubj

test-all:
	uv run pytest tests/ eval/ -v

# -----------------------------------------------------------------------------
# Development
# -----------------------------------------------------------------------------

scenario:
	uv run python -m generator run --scenario baseline_normal --count 10

build-policy-index:
	uv run python -m app.retrieval.corpus_loader

install-hooks:
	uv run pre-commit install

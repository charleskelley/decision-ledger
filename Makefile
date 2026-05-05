# =============================================================================
# DecisionLedger Makefile
# All Python commands run via `uv run` — never call python or pytest directly.
# =============================================================================

.DEFAULT_GOAL := help

.PHONY: help lint lint-sql typecheck test test-smoke test-integration test-replay \
        eval test-all check verify verify-ready verify-push scenario \
        build-policy-index install-hooks train eval-model

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
	@echo "    verify            Full local gate (check + integration + smoke; needs Docker)"
	@echo "    verify-ready      Probe whether Docker + services are up"
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

# verify-ready: probe local infra without running tests. Exits non-zero if
# Docker daemon is down or no services are running, with a clear hint.
# Always exits 0 when ready, so it composes with `verify`.
verify-ready:
	@if ! docker info >/dev/null 2>&1; then \
		echo "✗ Docker daemon not running. Start it (Docker Desktop / OrbStack)."; \
		exit 1; \
	fi
	@if [ -z "$$(docker compose ps -q 2>/dev/null)" ]; then \
		echo "✗ Compose stack not running. Start it with: docker compose up -d"; \
		exit 1; \
	fi
	@echo "✓ Docker daemon up; compose services running."

# verify: full local gate. Strict — fails loudly if Docker isn't ready.
# Use this manually before pushing changes that touch infra/, app/audit/,
# app/retrieval/, or scenario YAMLs.
verify: verify-ready check test-integration test-smoke
	@echo "✓ verify: all local gates green"

# verify-push: lenient gate invoked by the pre-push hook. Runs the full
# verify when Docker is up; falls back to `check` only when Docker is
# down (so pure-docs pushes aren't blocked). CI is the safety net.
verify-push:
	@if docker info >/dev/null 2>&1 \
		&& [ -n "$$(docker compose ps -q 2>/dev/null)" ]; then \
		echo "→ Docker up — running full verify before push."; \
		$(MAKE) check test-integration test-smoke; \
	else \
		echo "↷ Docker stack not running — running 'check' only."; \
		echo "  Integration validated by CI. Use 'make verify-ready' to start Docker."; \
		$(MAKE) check; \
	fi

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
	uv run python -m eval.runners.harness

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
	uv run pre-commit install --hook-type pre-push

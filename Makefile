# =============================================================================
# DecisionLedger Makefile
# All Python commands run via `uv run` — never call python or pytest directly.
# =============================================================================

.DEFAULT_GOAL := help

# Per-reasoner targets. Each reasoners/<domain>/reasoner.mk defines suffixed
# targets (train-<domain>, prepare-<domain>, eval-<domain>, ...) and appends
# them to the REASONER_*_TARGETS aggregates below. This file never names a
# reasoner. A reference-pattern mock (DR-35) appends its eval to
# MOCK_EVAL_TARGETS instead of REASONER_EVAL_TARGETS, so `make eval` never
# runs it, and a member that ships rule sets for the rules gate appends its
# rulesets/ directory to RULESET_DIRS.
REASONER_TRAIN_TARGETS   :=
REASONER_INDEX_TARGETS   :=
REASONER_PREPARE_TARGETS :=
REASONER_EVAL_TARGETS    :=
MOCK_EVAL_TARGETS        :=
RULESET_DIRS             :=
REASONER_HELP            :=
-include $(wildcard reasoners/*/reasoner.mk)

# Per-gate targets. Each gates/<name>/gate.mk defines targets suffixed with
# the gate directory name (prepare-gate-<name>, eval-gate-<name>, ...) and
# appends them to the GATE_*_TARGETS aggregates below. This file never names
# a gate.
GATE_PREPARE_TARGETS :=
GATE_EVAL_TARGETS    :=
GATE_HELP            :=
-include $(wildcard gates/*/gate.mk)

# DDL linted by lint-sql: the framework's, plus every gate's own.
SQL_DIRS := app/audit/sql $(wildcard gates/*/src/*/sql)

# Where prepare-rulesets stages the members' rule sets: the one directory a
# ledger process reads them from (RULES_GATE_RULESETS_DIR).
RULESETS_STAGE := outputs/stage/rulesets

.PHONY: help sync lint lint-sql typecheck test test-smoke test-integration \
        eval test-all check verify verify-ready verify-push \
        build-policy-index install-hooks train prepare-reasoners prepare-gates \
        prepare-rulesets eval-mocks schemas conformance serve

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
	@echo "    test-all          All test suites"
	@echo "    verify            Full local gate (check + integration + smoke; needs Docker)"
	@echo "    verify-ready      Probe whether Docker + services are up"
	@echo ""
	@echo "  Reasoners (aggregates over every reasoners/*/reasoner.mk)"
	@echo "    train               Train every reasoner's model"
	@echo "    build-policy-index  Index every reasoner's policy corpus (into the gate that serves it)"
	@echo "    prepare-reasoners   Index corpora + train missing models (pre-integration)"
	@echo "    prepare-rulesets    Stage every member's rulesets/ in $(RULESETS_STAGE) (RULES_GATE_RULESETS_DIR)"
	@echo "    eval                Every reasoner's eval, then every gate's (slow, costs money)"
	@echo "    eval-mocks          Every reference-pattern mock's eval (Postgres only; no model calls)"
	@echo ""
	@echo "  Per-reasoner targets"
	@for line in $(REASONER_HELP); do echo "$$line"; done
	@echo ""
	@echo "  Gates (aggregates over every gates/*/gate.mk)"
	@echo "    prepare-gates       Apply every gate's own Postgres DDL (its schema)"
	@echo ""
	@echo "  Per-gate targets"
	@for line in $(GATE_HELP); do echo "$$line"; done
	@echo ""
	@echo "  Ledger API"
	@echo "    serve             Run the ledger API (uvicorn, port 8000)"
	@echo "    schemas           Regenerate schemas/*.json from the models (CI diffs them)"
	@echo "    conformance       HTTP contract suite against DECISIONLEDGER_URL (see conformance/)"
	@echo ""
	@echo "  Development"
	@echo "    sync              Install the framework + every workspace reasoner, gate, and integration (uv sync)"
	@echo "    install-hooks     Install pre-commit git hooks"
	@echo ""

# -----------------------------------------------------------------------------
# Quality
# -----------------------------------------------------------------------------

lint:
	uv run ruff check --fix .
	uv run ruff format .

lint-sql:
	uv run sqlfluff lint --dialect postgres $(SQL_DIRS)

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
# Use this manually before pushing changes that touch app/audit/,
# docker-compose.yml, or a reasoner's or gate's package.
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

# pytest testpaths covers tests/, reasoners/*/tests, and gates/*/tests.
test:
	uv run pytest -m "not integration and not evaluation and not slow and not smoke" -v

test-smoke:
	uv run pytest -m smoke -v

test-integration:
	uv run pytest -m integration -v

test-all:
	uv run pytest -v

# -----------------------------------------------------------------------------
# Development
# -----------------------------------------------------------------------------

sync:
	uv sync

train: $(REASONER_TRAIN_TARGETS)

build-policy-index: $(REASONER_INDEX_TARGETS)

prepare-reasoners: $(REASONER_PREPARE_TARGETS)

prepare-gates: $(GATE_PREPARE_TARGETS)

# Rule sets are deployment config: the rules gate reads one directory
# (RULES_GATE_RULESETS_DIR) when a ledger process builds it, so this stages
# every member's rulesets/ (RULESET_DIRS) there, afresh, refusing two files of
# one name. Point the ledger at it before `make serve`:
#   export RULES_GATE_RULESETS_DIR=$(pwd)/outputs/stage/rulesets
# While no member ships a rule set it stages nothing, but still leaves the
# (empty) directory, so that export never names a missing one (the rules gate
# refuses to start on a directory that does not exist).
prepare-rulesets:
	@rm -rf $(RULESETS_STAGE) && mkdir -p $(RULESETS_STAGE)
	@for dir in $(RULESET_DIRS); do \
		for file in "$$dir"/*.yaml "$$dir"/*.yml; do \
			[ -e "$$file" ] || continue; \
			name="$$(basename "$$file")"; \
			if [ -e "$(RULESETS_STAGE)/$$name" ]; then \
				echo "prepare-rulesets: two members ship $$name" >&2; exit 1; \
			fi; \
			cp "$$file" "$(RULESETS_STAGE)/$$name"; \
		done; \
	done
	@if [ -z "$$(ls -A $(RULESETS_STAGE))" ]; then \
		echo "prepare-rulesets: no member ships a rule set; $(RULESETS_STAGE) is empty"; \
	else \
		echo "prepare-rulesets: staged in $(RULESETS_STAGE):" && ls -1 $(RULESETS_STAGE); \
	fi
	@echo "export RULES_GATE_RULESETS_DIR=$(abspath $(RULESETS_STAGE))"

eval: $(REASONER_EVAL_TARGETS) $(GATE_EVAL_TARGETS)

# The reference-pattern mocks' evals (DR-35), outside `make eval`: each
# composes the ledger over Postgres with the mock's own gate doubles, so it
# needs `docker compose up -d` but makes no model call.
eval-mocks: $(MOCK_EVAL_TARGETS)

install-hooks:
	uv run pre-commit install
	uv run pre-commit install --hook-type pre-push

# -----------------------------------------------------------------------------
# Ledger API
# -----------------------------------------------------------------------------

serve:
	uv run uvicorn --factory app.main:create_app --port $(or $(PORT),8000)

schemas:
	uv run python -m app.schemas --write

# The conformance suite runs against a live instance (DECISIONLEDGER_URL and
# the keys in the environment; see conformance/README.md). Not part of `test`.
conformance:
	uv run pytest conformance -m conformance -v

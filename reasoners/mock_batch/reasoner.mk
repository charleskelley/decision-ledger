# =============================================================================
# Batch mock Make targets — included by the root Makefile via
#   -include $(wildcard reasoners/*/reasoner.mk)
# Recipes run from the repo root. Each target is suffixed with the reasoner
# directory name. The mock ships no corpus and trains nothing, so it adds no
# prepare target. Its rule set (rulesets/mock-batch-v1.yaml) is deployment
# configuration the rules gate reads from RULES_GATE_RULESETS_DIR when a ledger
# process builds it: the member's rulesets/ directory joins RULESET_DIRS, which
# `make prepare-rulesets` stages in outputs/stage/rulesets. A reference-pattern
# mock (DR-35) never joins `make eval`: its eval is appended to
# MOCK_EVAL_TARGETS (`make eval-mocks`, Postgres only, no model calls).
# =============================================================================

MOCK_BATCH_REASONER_ID := mock-batch-reasoner
MOCK_BATCH_DIR := reasoners/mock_batch

.PHONY: eval-mock_batch run-mock_batch registration-mock_batch

RULESET_DIRS      += $(MOCK_BATCH_DIR)/rulesets
MOCK_EVAL_TARGETS += eval-mock_batch

REASONER_HELP += "  mock_batch ($(MOCK_BATCH_REASONER_ID), a reference pattern: the daily batch scorer)"
REASONER_HELP += "    eval-mock_batch                     Framework eval with the mock's adapter and the rules gate (part of eval-mocks)"
REASONER_HELP += "    run-mock_batch                      Score a synthetic day and submit it (COUNT=25000; MOCK_BATCH_LEDGER_URL, _KEY)"
REASONER_HELP += "    registration-mock_batch             Regenerate $(MOCK_BATCH_DIR)/registration.yaml"

eval-mock_batch:
	uv run python -m eval.runners.harness --reasoner-id $(MOCK_BATCH_REASONER_ID)

# Score a synthetic day and submit it to MOCK_BATCH_LEDGER_URL with
# MOCK_BATCH_LEDGER_KEY (a blank URL refuses to run): the records in batches of
# at most 10,000 lines, the exceptions one at a time for the rules gate.
run-mock_batch:
	uv run python -m mock_batch_reasoner run --count $(or $(COUNT),25000)

registration-mock_batch:
	uv run python -m mock_batch_reasoner registration-yaml > $(MOCK_BATCH_DIR)/registration.yaml

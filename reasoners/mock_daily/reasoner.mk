# =============================================================================
# Daily mock Make targets — included by the root Makefile via
#   -include $(wildcard reasoners/*/reasoner.mk)
# Recipes run from the repo root. Each target is suffixed with the reasoner
# directory name. The mock ships no corpus and trains nothing, so it adds no
# prepare target. Its rule set (rulesets/mock-daily-v1.yaml) is deployment
# configuration the rules gate reads from RULES_GATE_RULESETS_DIR when a ledger
# process builds it: the member's rulesets/ directory joins RULESET_DIRS, which
# `make prepare-rulesets` stages in outputs/stage/rulesets. The mock binds the
# rules gate mandatory, so a ledger that has not staged the rule set holds
# every one of its decisions, recorded ones too. A reference-pattern mock
# (DR-35) never joins `make eval`: its eval is appended to MOCK_EVAL_TARGETS
# (`make eval-mocks`, Postgres only, no model calls).
# =============================================================================

MOCK_DAILY_REASONER_ID := mock-daily-reasoner
MOCK_DAILY_DIR := reasoners/mock_daily

.PHONY: eval-mock_daily run-mock_daily registration-mock_daily

RULESET_DIRS      += $(MOCK_DAILY_DIR)/rulesets
MOCK_EVAL_TARGETS += eval-mock_daily

REASONER_HELP += "  mock_daily ($(MOCK_DAILY_REASONER_ID), a reference pattern: the deterministic computation with exceptions)"
REASONER_HELP += "    eval-mock_daily                     Framework eval with the mock's adapter and the rules gate (part of eval-mocks)"
REASONER_HELP += "    run-mock_daily                      Reconcile a synthetic day and submit it (COUNT=1000; MOCK_DAILY_LEDGER_URL, _KEY)"
REASONER_HELP += "    registration-mock_daily             Regenerate $(MOCK_DAILY_DIR)/registration.yaml"

eval-mock_daily:
	uv run python -m eval.runners.harness --reasoner-id $(MOCK_DAILY_REASONER_ID)

# Reconcile a synthetic day and submit it to MOCK_DAILY_LEDGER_URL with
# MOCK_DAILY_LEDGER_KEY (a blank URL refuses to run), one decision per row:
# the accounts within tolerance recorded, the exceptions gated, and the rules
# gate (mandatory) on every one.
run-mock_daily:
	uv run python -m mock_daily_reasoner run --count $(or $(COUNT),1000)

registration-mock_daily:
	uv run python -m mock_daily_reasoner registration-yaml > $(MOCK_DAILY_DIR)/registration.yaml

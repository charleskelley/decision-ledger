# =============================================================================
# ATO reasoner Make targets — included by the root Makefile via
#   -include $(wildcard reasoners/*/reasoner.mk)
# Recipes run from the repo root. Each target is suffixed with the reasoner
# directory name; the root aggregates (train, build-policy-index,
# prepare-reasoners, eval) fan out through the REASONER_*_TARGETS variables.
# =============================================================================

ATO_REASONER_ID := ato-reasoner
ATO_MODEL_PATH  := reasoners/account_takeover/src/ato_reasoner/scorer/models/ato-v1.ubj

.PHONY: train-account_takeover eval-model-account_takeover \
        scenario-account_takeover build-policy-index-account_takeover \
        prepare-account_takeover eval-account_takeover \
        capture-baselines-account_takeover

REASONER_TRAIN_TARGETS   += train-account_takeover
REASONER_INDEX_TARGETS   += build-policy-index-account_takeover
REASONER_PREPARE_TARGETS += prepare-account_takeover
REASONER_EVAL_TARGETS    += eval-account_takeover

REASONER_HELP += "  account_takeover ($(ATO_REASONER_ID))"
REASONER_HELP += "    train-account_takeover              Train the XGBoost scorer (artifact + card + CSVs)"
REASONER_HELP += "    eval-model-account_takeover         Evaluate the trained scorer on a fresh-seed test set"
REASONER_HELP += "    scenario-account_takeover           Publish 10 baseline_normal login events"
REASONER_HELP += "    build-policy-index-account_takeover Embed and index the ATO policy corpus"
REASONER_HELP += "    prepare-account_takeover            Index the corpus; train the scorer if missing"
REASONER_HELP += "    eval-account_takeover               Eval harness with the ATO adapter (slow, paid)"
REASONER_HELP += "    capture-baselines-account_takeover  Re-capture faithfulness fixtures (live gate)"

train-account_takeover:
	uv run python -m ato_reasoner.scorer train \
		--output $(ATO_MODEL_PATH) \
		--samples 5000

eval-model-account_takeover:
	uv run python -m ato_reasoner.scorer eval --model $(ATO_MODEL_PATH)

scenario-account_takeover:
	uv run python -m ato_reasoner.generator run --scenario baseline_normal --count 10

build-policy-index-account_takeover:
	uv run python -m app.retrieval.corpus_loader --reasoner-id $(ATO_REASONER_ID)

# Index + train-if-missing: everything a fresh checkout (or CI) needs before
# integration tests or the eval harness can run against this reasoner.
prepare-account_takeover: build-policy-index-account_takeover
	@if [ ! -f $(ATO_MODEL_PATH) ]; then \
		$(MAKE) train-account_takeover; \
	else \
		echo "✓ scorer artifact present: $(ATO_MODEL_PATH)"; \
	fi

eval-account_takeover:
	uv run python -m eval.runners.harness --reasoner-id $(ATO_REASONER_ID)

capture-baselines-account_takeover:
	uv run python -m ato_reasoner.eval.capture_baselines

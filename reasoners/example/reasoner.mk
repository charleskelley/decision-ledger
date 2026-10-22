# =============================================================================
# Example reasoner Make targets — included by the root Makefile via
#   -include $(wildcard reasoners/*/reasoner.mk)
# Recipes run from the repo root. Each target is suffixed with the reasoner
# directory name; the root aggregates (build-policy-index, prepare-reasoners,
# eval) fan out through the REASONER_*_TARGETS variables. The example's
# corpus is loaded into the RAG policy gate, under the corpus id its binding
# names (example-policy, DR-34), by that gate's
# build-policy-index-<reasoner_id> target (gates/policy_rag/gate.mk). It has
# no model to train: its scorer is a documented heuristic.
# =============================================================================

EXAMPLE_REASONER_ID := example-reasoner
POLICY_CORPUS_ID_example-reasoner := example-policy

.PHONY: prepare-example eval-example sample-example batch-example

REASONER_INDEX_TARGETS   += build-policy-index-$(EXAMPLE_REASONER_ID)
REASONER_PREPARE_TARGETS += prepare-example
REASONER_EVAL_TARGETS    += eval-example

REASONER_HELP += "  example ($(EXAMPLE_REASONER_ID))"
REASONER_HELP += "    build-policy-index-$(EXAMPLE_REASONER_ID)  Load the example's three-document corpus as example-policy (RAG policy gate)"
REASONER_HELP += "    prepare-example                     Index the corpus (the scorer needs no training)"
REASONER_HELP += "    eval-example                        Framework eval with the example adapter (shadow re-decides via the LLM gate)"
REASONER_HELP += "    sample-example                      Print one wire submission (BAND=low|mid|high)"
REASONER_HELP += "    batch-example                       Print an NDJSON batch of records (COUNT=1000)"

prepare-example: build-policy-index-$(EXAMPLE_REASONER_ID)

eval-example:
	uv run python -m eval.runners.harness --reasoner-id $(EXAMPLE_REASONER_ID)

sample-example:
	@uv run python -m example_reasoner sample --band $(or $(BAND),low)

batch-example:
	@uv run python -m example_reasoner batch --count $(or $(COUNT),1000)

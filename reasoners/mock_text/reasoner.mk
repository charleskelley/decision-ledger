# =============================================================================
# Text mock Make targets — included by the root Makefile via
#   -include $(wildcard reasoners/*/reasoner.mk)
# Recipes run from the repo root. Each target is suffixed with the reasoner
# directory name. The mock's binding names two corpora (DR-34), and the corpus
# loader loads one directory as one corpus id, so its two subdirectories are
# loaded by two loader calls, each with --corpus-id and --corpus-dir:
# regulatory/ as mock-text-regulatory (tier 1) and practice/ as
# mock-text-practice (tier 2). The mock therefore defines no
# POLICY_CORPUS_ID_mock-text-reasoner: the RAG policy gate's
# build-policy-index-<reasoner_id> target loads a reasoner's whole corpus
# directory as one corpus, which would merge both subtrees. `make
# build-policy-index` and `make prepare-reasoners` load both corpora with
# every other reasoner's. A reference-pattern mock (DR-35) never joins
# `make eval`: its eval is appended to MOCK_EVAL_TARGETS (`make eval-mocks`,
# Postgres only, no model calls).
# =============================================================================

MOCK_TEXT_REASONER_ID := mock-text-reasoner
MOCK_TEXT_DIR := reasoners/mock_text
MOCK_TEXT_ASSETS := $(MOCK_TEXT_DIR)/src/mock_text_reasoner/assets

.PHONY: index-mock_text prepare-mock_text eval-mock_text registration-mock_text \
        assets-mock_text

REASONER_INDEX_TARGETS   += index-mock_text
REASONER_PREPARE_TARGETS += prepare-mock_text
MOCK_EVAL_TARGETS        += eval-mock_text

REASONER_HELP += "  mock_text ($(MOCK_TEXT_REASONER_ID), a reference pattern: the LLM-native classifier)"
REASONER_HELP += "    index-mock_text                     Load regulatory/ as mock-text-regulatory and practice/ as mock-text-practice (RAG policy gate)"
REASONER_HELP += "    prepare-mock_text                   Index both corpora (the classifier needs no training)"
REASONER_HELP += "    eval-mock_text                      Framework eval with the mock's adapter and gate double (part of eval-mocks)"
REASONER_HELP += "    registration-mock_text              Regenerate $(MOCK_TEXT_DIR)/registration.yaml"
REASONER_HELP += "    assets-mock_text                    Copy its prompts and corpora under ASSETS_ROOT (outputs/stage/assets)"

# One loader call per corpus the binding names; each reads one subdirectory.
index-mock_text:
	uv run python -m policy_rag_gate.corpus_loader --corpus-id mock-text-regulatory \
		--reasoner-id $(MOCK_TEXT_REASONER_ID) --corpus-dir $(MOCK_TEXT_ASSETS)/corpus/regulatory
	uv run python -m policy_rag_gate.corpus_loader --corpus-id mock-text-practice \
		--reasoner-id $(MOCK_TEXT_REASONER_ID) --corpus-dir $(MOCK_TEXT_ASSETS)/corpus/practice

prepare-mock_text: index-mock_text

eval-mock_text:
	uv run python -m eval.runners.harness --reasoner-id $(MOCK_TEXT_REASONER_ID)

registration-mock_text:
	uv run python -m mock_text_reasoner registration-yaml > $(MOCK_TEXT_DIR)/registration.yaml

# The mock's directory under a RAG policy gate asset root, for a ledger that
# registers it by file or over the API (its plugin excluded): point
# POLICY_RAG_GATE_ASSETS_ROOT at $(ASSETS_ROOT) (default outputs/stage/assets),
# and load each corpus from its copy (one loader call each, --corpus-dir
# <root>/mock-text-reasoner/corpus/regulatory and .../practice).
assets-mock_text:
	@root="$(or $(ASSETS_ROOT),outputs/stage/assets)"; \
	rm -rf "$$root/$(MOCK_TEXT_REASONER_ID)" && mkdir -p "$$root/$(MOCK_TEXT_REASONER_ID)" && \
	cp -R $(MOCK_TEXT_ASSETS)/. "$$root/$(MOCK_TEXT_REASONER_ID)/" && \
	echo "assets-mock_text: $$root/$(MOCK_TEXT_REASONER_ID):" && ls -R "$$root/$(MOCK_TEXT_REASONER_ID)"

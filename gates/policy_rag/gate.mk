# =============================================================================
# RAG policy gate Make targets — included by the root Makefile via
#   -include $(wildcard gates/*/gate.mk)
# Recipes run from the repo root. Targets are suffixed with the gate directory
# name (policy_rag); the root aggregates (prepare-gates, eval) fan out through
# the GATE_*_TARGETS variables. The gate is reasoner-agnostic: a corpus is
# loaded under the corpus id reasoners' bindings name (DR-34), and the eval
# runs against every installed reasoner eval adapter that ships datasets for
# this gate.
# =============================================================================

POLICY_RAG_GATE_ID := policy

.PHONY: prepare-gate-policy_rag eval-gate-policy_rag

GATE_PREPARE_TARGETS += prepare-gate-policy_rag
GATE_EVAL_TARGETS    += eval-gate-policy_rag

GATE_HELP += "  policy_rag ($(POLICY_RAG_GATE_ID))"
GATE_HELP += "    prepare-gate-policy_rag           Apply the gate's Postgres DDL (policy_rag schema, vector extension)"
GATE_HELP += "    build-policy-index-<reasoner_id>  Load the corpus a reasoner's binding names (POLICY_CORPUS_ID_<reasoner_id>)"
GATE_HELP += "    eval-gate-policy_rag              Gate eval: retrieval, faithfulness, citation (slow, paid; REASONER=<id> narrows)"

prepare-gate-policy_rag:
	uv run python -m policy_rag_gate.schema

# Load the corpus a reasoner's binding to this gate names (DR-34), under that
# corpus id, from where the gate reads the reasoner's corpus: the composed
# reasoner plugin's corpus_dir, else <POLICY_RAG_GATE_ASSETS_ROOT>/<reasoner_id>/corpus
# (DR-33). Each reasoner.mk sets POLICY_CORPUS_ID_<reasoner_id>; the gate
# refuses a binding whose corpus is not loaded, so load it before the
# ledger starts or the registration is written.
build-policy-index-%:
	$(if $(POLICY_CORPUS_ID_$*),,$(error set POLICY_CORPUS_ID_$* in the reasoner's reasoner.mk to the corpus id its binding names))
	uv run python -m policy_rag_gate.corpus_loader --corpus-id $(POLICY_CORPUS_ID_$*) --reasoner-id $*

eval-gate-policy_rag:
	uv run python -m policy_rag_gate.eval $(if $(REASONER),--reasoner-id $(REASONER))

# =============================================================================
# RAG policy gate Make targets — included by the root Makefile via
#   -include $(wildcard gates/*/gate.mk)
# Recipes run from the repo root. Targets are suffixed with the gate directory
# name (policy_rag); the root aggregates (prepare-gates, eval) fan out through
# the GATE_*_TARGETS variables. The gate is reasoner-agnostic: a reasoner's
# corpus is indexed by its reasoner_id, and the eval runs against every
# installed reasoner eval adapter that ships datasets for this gate.
# =============================================================================

POLICY_RAG_GATE_ID := policy

.PHONY: prepare-gate-policy_rag eval-gate-policy_rag

GATE_PREPARE_TARGETS += prepare-gate-policy_rag
GATE_EVAL_TARGETS    += eval-gate-policy_rag

GATE_HELP += "  policy_rag ($(POLICY_RAG_GATE_ID))"
GATE_HELP += "    prepare-gate-policy_rag           Apply the gate's Postgres DDL (policy_rag schema, vector extension)"
GATE_HELP += "    build-policy-index-<reasoner_id>  Embed and index one reasoner's policy corpus into the gate's stores"
GATE_HELP += "    eval-gate-policy_rag              Gate eval: retrieval, faithfulness, citation (slow, paid; REASONER=<id> narrows)"

prepare-gate-policy_rag:
	uv run python -m policy_rag_gate.schema

# Index one reasoner's corpus: the loader resolves the corpus directory from
# the installed reasoner plugin whose registration has this reasoner_id.
build-policy-index-%:
	uv run python -m policy_rag_gate.corpus_loader --reasoner-id $*

eval-gate-policy_rag:
	uv run python -m policy_rag_gate.eval $(if $(REASONER),--reasoner-id $(REASONER))

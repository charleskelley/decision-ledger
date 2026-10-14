"""The RAG policy gate's eval: retrieval, faithfulness, citation, and the judge.

The gate owns its eval (DR-28). Three dimensions measure what this gate
does, each a framework ``eval.dimensions.Dimension`` reported in the
framework's ``EvalReport``:

    retrieval:     Retrieval quality — precision/recall@k, MRR, version and
                   jurisdiction-filter accuracy — against the gate's own
                   hybrid retriever, tenant-filtered.
    faithfulness:  Generation faithfulness — RAGAS plus an LLM judge.
    citation:      Citation accuracy — relevance and entailment judges.

``judge`` is the LLM-as-judge primitive and ``ragas`` the RAGAS adapter; the
judge prompts ship in ``prompts/judges/``. ``harness`` (``python -m
policy_rag_gate.eval``) runs the three against the golden datasets each
reasoner ships for this gate. The framework's own harness runs the
reasoner-facing dimensions (decision consistency, adversarial robustness).

The judges and RAGAS call LLMs: the harness is slow and costs money.
"""

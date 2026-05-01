"""Evaluation harness for DecisionLedger — 5-dimension behavioral evaluation framework.

Implements the evaluation framework that gates every release candidate. All five
dimensions must pass defined CI thresholds before a release can proceed.

Dimensions:
    01 — Retrieval Quality:       Context Precision/Recall @k, MRR, version accuracy.
    02 — Generation Faithfulness: RAGAS Faithfulness, hallucination rate.
    03 — Decision Consistency:    Action stability across 8 scenarios x 3 orderings.
    04 — Citation Accuracy:       Citation relevance, claim-citation entailment.
    05 — Adversarial Robustness:  Injection resistance, schema violation handling.

Imports from core/ for dimension contracts. Imports from app/ to drive the live
pipeline.
Tests are marked @pytest.mark.evaluation — they invoke the LLM and cost money.
Do not run in a tight loop.

Usage::

    make eval                    # Full harness (all 5 dimensions)
    pytest eval/ -m evaluation   # Same via pytest directly
"""

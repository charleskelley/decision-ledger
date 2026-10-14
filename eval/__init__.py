"""Evaluation harness for DecisionLedger — the framework's behavioral dimensions.

Evaluates one reasoner's decision pipeline, end to end, against its golden
datasets. A failing dimension blocks the release candidate.

Framework dimensions (driven through the reasoner's own pipeline):
    Decision Consistency:    Action stability across scenarios x orderings.
    Adversarial Robustness:  Injection resistance, schema-violation handling,
                             novel patterns, fallback paths.

Gates own the dimensions that measure what a gate does (DR-28): a retrieving
gate's retrieval quality, an LLM gate's faithfulness and citation accuracy.
Each gate's own harness runs them and reports in the same ``EvalReport``
schema, through ``eval.runners.harness.run_eval``.

Imports from core/ for dimension contracts. Imports from app/ to drive the live
pipeline. The live harness calls whatever models the reasoner's gates use and
can cost money. Do not run in a tight loop.

Usage::

    make eval-<reasoner>         # this harness for one reasoner
    make eval                    # every reasoner, then every gate's eval
"""

# `eval`

The framework's evaluation harness — decision consistency, adversarial
robustness, and the shadow dimension (the advisory gates run offline on a
sample of recorded decisions), driven through a reasoner's own pipeline. It is
reasoner-agnostic: each reasoner's event type, golden datasets, and
pipeline driver arrive through an [`EvalAdapter`][eval.adapter.EvalAdapter]
discovered under the `decisionledger.eval_adapters` entry point group.
Each dimension lives in `eval/dimensions/`; the harness driver lives
in [`eval.runners.harness`][eval.runners.harness]. Gates own the
dimensions that measure a gate (DR-28): the RAG policy gate's retrieval,
faithfulness, and citation dimensions run in its own harness
(`gates/policy_rag`, `python -m policy_rag_gate.eval`), which reuses
`run_eval` and the `EvalReport` schema.

The `EvalReport` schema and dimension-result types live in
`core.eval` so enforcement and audit can reference them without
importing the harness.

::: eval
    options:
      show_root_heading: false

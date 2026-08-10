# `eval`

5D evaluation harness — retrieval quality, generation faithfulness,
decision consistency, citation accuracy, adversarial robustness.
Each dimension lives in `eval/dimensions/`; the harness driver lives
in [`eval.runners.harness`][eval.runners.harness].

The `EvalReport` schema and dimension-result types live in
`core.eval` so enforcement and audit can reference them without
importing the harness.

::: eval
    options:
      show_root_heading: false

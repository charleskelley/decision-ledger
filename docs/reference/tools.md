# `tools`

Tracked, reusable tooling that lives outside the runtime path —
small CLIs and scripts whose outputs feed the docs, eval, or
training loops.

Today this is just `capture_baselines.py`, which captures live
faithfulness fixtures from the running stack and merges them into
`eval/datasets/faithfulness/golden_outputs.yaml` by `case_id`. Run
via `make capture-baselines`.

::: tools
    options:
      show_root_heading: false

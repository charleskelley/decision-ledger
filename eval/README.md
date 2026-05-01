# Eval harness

The 5-dimension evaluation harness for DecisionLedger. Run with:

```bash
make eval
```

This invokes `eval/runners/harness.py:main`, which constructs the default
dimension set, runs each dimension concurrently, assembles an
`EvalReport`, writes it to disk, and exits with a code reflecting
threshold pass/fail.

## Output paths

| Path | Purpose | Tracked? |
|---|---|---|
| `outputs/stage/eval/eval-report.json` | Default — overwritten by every `make eval` | gitignored |
| `outputs/eval/eval-report-v<N>.json` | Tracked baselines, explicitly promoted from staging | tracked |

Override the default with `--output PATH` or `EVAL_OUTPUT_PATH=…`; the
flag wins when both are set.

To capture a baseline as a tracked receipt:

```bash
make eval
cp outputs/stage/eval/eval-report.json outputs/eval/eval-report-v<N>.json
git add outputs/eval/eval-report-v<N>.json
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All dimensions passed thresholds (`overall_passed=true`) |
| 1 | One or more dimensions failed thresholds |
| 2 | No dimensions registered (config error; no report file written) |

## Required env vars

`OPENAI_API_KEY` — required after MVP plan Step 3 wires the
faithfulness and citation dimensions. Currently no env vars are
required (no dimensions wired; harness exits 2).

## EvalReport JSON schema

Source of truth: [`core/eval/metrics.py`](../core/eval/metrics.py)
(`EvalReport`).

Top-level fields:

- `run_id` — UUID for this evaluation run.
- `created_at` — ISO-8601 UTC timestamp.
- `overall_passed` — `true` only when every dimension passed.
- `dimensions` — list of `DimensionResult` (per-dimension pass/fail,
  threshold violations, sample count, evaluated-at timestamp).
- `retrieval`, `faithfulness`, `consistency`, `citation`, `robustness`
  — typed metric blocks; `null` when the corresponding dimension did
  not run.

See `core/eval/metrics.py` for the per-dimension metric field
definitions.

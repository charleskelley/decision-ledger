# Eval harness

The 5-dimension evaluation harness for DecisionLedger. Run with:

```bash
make eval                         # one harness run per installed reasoner
make eval-account_takeover        # just the ATO reasoner
# or, for any installed reasoner:
uv run python -m eval.runners.harness --reasoner-id <reasoner-id>
```

This invokes `eval/runners/harness.py:main`, which loads the named
reasoner's eval adapter, constructs the default dimension set, runs each
dimension concurrently, assembles an `EvalReport`, writes it to disk, and
exits with a code reflecting threshold pass/fail.

## Reasoner eval adapters

The harness is reasoner-agnostic. Everything reasoner-specific comes from
an `EvalAdapter` (`eval/adapter.py`) that the reasoner publishes under the
`decisionledger.eval_adapters` entry point group, named by its
`reasoner_id`:

| Adapter field | Used for |
|---|---|
| `reasoner_id` | Tenant filter for the retrieval dimension; must equal the entry point name |
| `event_type` | Pydantic model the consistency and robustness datasets are validated against |
| `datasets_dir` | Root of the golden datasets: `retrieval/`, `faithfulness/`, `scenarios/`, `citations/`, `robustness/` |
| `build_driver(settings)` | Returns an `EvalDriver` into the reasoner's live pipeline |

The reasoner is chosen with `--reasoner-id` or the
`DECISIONLEDGER_EVAL_REASONER` env var. A missing or unknown id exits 2
and lists the installed adapters.

The ATO reasoner's adapter is `ato_reasoner.eval.adapter:ADAPTER`; its
driver (`ato_reasoner/eval/driver.py`), golden datasets
(`ato_reasoner/eval/datasets/`), and fixture re-capture script
(`make capture-baselines-account_takeover`) all live in
`reasoners/account_takeover/src/ato_reasoner/eval/`.

## Output paths

| Path | Purpose | Tracked? |
|---|---|---|
| `outputs/stage/eval/eval-report.json` | Default — overwritten by every `make eval` | gitignored |
| `outputs/eval/eval-report-v<N>.json` | Tracked baselines, explicitly promoted from staging | tracked |

Every run writes the same default path, so when `make eval` covers more
than one reasoner, the last run's report wins; pass `--output` per
reasoner to keep each.

The tracked `eval-report-v2.json` (2026-10-11) is the current baseline: it
was produced with the `ato-v3` prompt and the `reasoner_id`-filtered
retrieval dimension after DR-26. `eval-report-v1.json` (2026-05-06,
`ato-v2`, unfiltered retrieval) is kept as history.

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
| 2 | Config error: no reasoner named, no eval adapter for it, missing API keys, or no dimensions registered (no report file written) |

## Required env vars

`OPENAI_API_KEY` (the policy gate inside the reasoner's pipeline) and
`ANTHROPIC_API_KEY` (the faithfulness and citation judges). The harness
exits 2 when either is missing.

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

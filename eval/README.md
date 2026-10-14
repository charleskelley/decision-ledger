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
| `outputs/stage/eval/reasoner/<reasoner_id>/eval-report.json` | Default for this harness — overwritten by each run for that reasoner | gitignored |
| `outputs/stage/eval/gate/<gate_id>/<reasoner_id>/eval-report.json` | Default for a gate's own harness (e.g. `make eval-gate-policy_rag`: retrieval, faithfulness, citation) | gitignored |
| `outputs/eval/<kind>/<id>/eval-report-v<N>.json` | Tracked baselines (`<kind>` is `reasoner` or `gate`), explicitly promoted from staging | tracked |
| `outputs/eval/eval-report-v<N>.json` | Tracked baselines from before the per-kind layout (history) | tracked |

Since Phase 1b of DR-28 the retrieval, faithfulness, and citation
dimensions belong to the RAG policy gate (`gates/policy_rag`) and run in its
own harness; this harness runs consistency and robustness. `make eval` runs
every reasoner's harness, then every gate's.

The tracked `eval-report-v2.json` (2026-10-11) is the current baseline: it
was produced with the `ato-v3` prompt and the `reasoner_id`-filtered
retrieval dimension after DR-26. `eval-report-v1.json` (2026-05-06,
`ato-v2`, unfiltered retrieval) is kept as history.

Override the default with `--output PATH` or `EVAL_OUTPUT_PATH=…`; the
flag wins when both are set.

To capture a baseline as a tracked receipt (per reasoner, and per gate):

```bash
make eval
cp outputs/stage/eval/reasoner/<reasoner_id>/eval-report.json \
   outputs/eval/reasoner/<reasoner_id>/eval-report-v<N>.json
cp outputs/stage/eval/gate/<gate_id>/<reasoner_id>/eval-report.json \
   outputs/eval/gate/<gate_id>/<reasoner_id>/eval-report-v<N>.json
git add outputs/eval/
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | All dimensions passed thresholds (`overall_passed=true`) |
| 1 | One or more dimensions failed thresholds |
| 2 | Config error: no reasoner named, no eval adapter for it, a driver configuration error (e.g. a missing API key the reasoner's gate needs), or no dimensions registered (no report file written) |

## Required env vars

This harness checks no key itself: the reasoner's driver fails fast on
what its own gates need (ATO's driver requires `OPENAI_API_KEY` for the
policy gate in its pipeline), and the CLI turns that into exit 2. The RAG
policy gate's own harness (`make eval-gate-policy_rag`) requires
`OPENAI_API_KEY` (RAGAS) and `ANTHROPIC_API_KEY` (the faithfulness and
citation judges) and exits 2 when either is missing.

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

# Eval harness

The framework's evaluation harness. It evaluates one reasoner per run by
driving that reasoner's own pipeline through the ledger, and measures what
the ledger decides:

- **consistency**: the same events in different orders reach the same final
  action (zero tolerance);
- **robustness**: injection resistance, schema-failure handling, novel
  adversarial patterns, and forced fallbacks;
- **shadow**: a sample of the decisions the run recorded, re-decided offline
  with the reasoner's advisory gates (report-only).

Dimensions that measure one gate (the RAG policy gate's retrieval,
faithfulness, and citation) belong to that gate and run in its own harness
(`python -m policy_rag_gate.eval`, `make eval-gate-policy_rag`), which reuses
this package's `run_eval` and the `EvalReport` schema. See
[`docs/design/evaluation.md`](../docs/design/evaluation.md) for the
dimensions, their thresholds, and their costs.

```bash
make eval-account_takeover        # the ATO reasoner
make eval-example                 # the example reasoner
make eval                         # every reasoner's harness, then every gate's
uv run python -m eval.runners.harness --reasoner-id <reasoner_id>
```

`eval/runners/harness.py:main` loads the named reasoner's eval adapter,
builds the dimensions over its datasets, runs consistency and robustness
concurrently and then shadow, writes the `EvalReport`, and exits with a code
that says whether every dimension passed.

## Layout

| Path | What |
|---|---|
| `adapter.py` | `EvalAdapter` and `EvalDriver` protocols; `load_eval_adapter` |
| `runners/harness.py` | `run_eval`, `assemble_report`, and the CLI |
| `dimensions/consistency.py` | Decision consistency |
| `dimensions/robustness.py` | Adversarial robustness |
| `dimensions/shadow.py` | Shadow evaluation of recorded decisions |
| `dimensions/skipped.py` | `SkippedDimension`, the stand-in for a dimension whose dataset is missing |

## Reasoner eval adapters

The harness is reasoner-agnostic. Everything reasoner-specific comes from an
`EvalAdapter` (`eval/adapter.py`) that the reasoner publishes under the
`decisionledger.eval_adapters` entry-point group, named by its
`reasoner_id`:

| Adapter member | Used for |
|---|---|
| `reasoner_id` | The reasoner evaluated; must equal the entry-point name |
| `event_type` | Pydantic model the consistency and robustness datasets are validated against |
| `datasets_dir` | Root of the golden datasets: `scenarios/*.yaml` and `robustness/{injection,malformed,novel,fallback}.yaml` for this harness, plus `retrieval/`, `faithfulness/`, and `citations/` for the RAG gate's |
| `build_driver(settings)` | Returns an `EvalDriver` into the reasoner's live pipeline; its `framework` services back the shadow dimension |

The reasoner is chosen with `--reasoner-id` or the
`DECISIONLEDGER_EVAL_REASONER` env var. A missing id exits 2; an unknown one
exits 2 and lists the installed adapters.

| Reasoner | Adapter | Driver and datasets |
|---|---|---|
| ATO | `ato_reasoner.eval.adapter:ADAPTER` | `reasoners/account_takeover/src/ato_reasoner/eval/` (`driver.py`, `datasets/`); `make capture-baselines-account_takeover` re-captures its faithfulness fixtures |
| Example | `example_reasoner.eval.adapter:ADAPTER` | `reasoners/example/src/example_reasoner/eval/` (`driver.py`, `datasets/`) |

A missing dataset becomes a `SkippedDimension`, which counts as a failure, so
a gap is visible in the report instead of disappearing.

## Output paths

| Path | Purpose | Tracked? |
|---|---|---|
| `outputs/stage/eval/reasoner/<reasoner_id>/eval-report.json` | Default for this harness; overwritten by each run for that reasoner | gitignored |
| `outputs/stage/eval/gate/policy/<reasoner_id>/eval-report.json` | Default for the RAG policy gate's harness | gitignored |
| `outputs/eval/<kind>/<id>/eval-report-v<N>.json` | Promoted baselines (`<kind>` is `reasoner` or `gate`; for a gate, `<id>` is `<gate_id>/<reasoner_id>`) | tracked |
| `outputs/eval/eval-report-v<N>.json` | Baselines from before the harness split (history) | tracked |

Override this harness's default with `--output PATH` or `EVAL_OUTPUT_PATH=…`;
the flag wins when both are set. The gate's harness takes `--output-dir`.

The two tracked reports, `eval-report-v1.json` (2026-05-06) and
`eval-report-v2.json` (2026-10-11), predate the split: one harness ran all
five dimensions for ATO, and every dimension passed. No baseline has been
promoted under the per-kind layout yet. To promote one (per reasoner, and
per gate):

```bash
make eval
cp outputs/stage/eval/reasoner/<reasoner_id>/eval-report.json \
   outputs/eval/reasoner/<reasoner_id>/eval-report-v<N>.json
cp outputs/stage/eval/gate/policy/<reasoner_id>/eval-report.json \
   outputs/eval/gate/policy/<reasoner_id>/eval-report-v<N>.json
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every dimension passed (`overall_passed=true`) |
| 1 | One or more dimensions failed or were skipped |
| 2 | Config error: no reasoner named, no eval adapter for it, a driver configuration error (for example a missing API key the reasoner's pipeline needs), or no dimensions; no report file is written |

## Required env vars

This harness checks no key itself: the reasoner's driver fails fast on what
its own pipeline needs, and the CLI turns that into exit 2. ATO's driver
requires `OPENAI_API_KEY`, because ATO's pipeline calls the RAG policy gate.
The example reasoner's shadow dimension also calls that gate's model. The
RAG policy gate's own harness requires `OPENAI_API_KEY` (RAGAS) and
`ANTHROPIC_API_KEY` (the faithfulness and citation judges) and exits 2 when
either is missing.

## EvalReport JSON schema

Source of truth: [`core/eval/metrics.py`](../core/eval/metrics.py)
(`EvalReport`).

Top-level fields:

- `run_id`: UUID for this evaluation run.
- `created_at`: ISO-8601 UTC timestamp.
- `overall_passed`: `true` only when every dimension passed.
- `dimensions`: list of `DimensionResult` (per-dimension pass or fail,
  metrics, threshold violations, sample count, evaluated-at timestamp).
- `retrieval`, `faithfulness`, `consistency`, `citation`, `robustness`,
  `shadow`: typed metric blocks; `null` when the corresponding dimension did
  not run. This harness fills `consistency`, `robustness`, and `shadow`; the
  RAG gate's fills the other three.

See `core/eval/metrics.py` for the per-dimension metric fields.

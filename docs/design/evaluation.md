# Evaluation

DecisionLedger evaluates at two levels, and each level owns its harness:

- **The framework harness** (`eval/`, `eval.runners.harness`) evaluates a
  *reasoner*: it drives the reasoner's own pipeline through the ledger and
  measures what the ledger decides. Its dimensions are decision consistency,
  adversarial robustness, and shadow evaluation.
- **A gate's own harness** evaluates *that gate*. The RAG policy gate's
  (`policy_rag_gate.eval`) measures retrieval quality, generation
  faithfulness, and citation accuracy against the datasets each reasoner
  ships for it
  ([DR-28](decisions.md#dr-28-gate-contract-and-gate-plugins)).

Both write the same `EvalReport` (`core/eval/metrics.py`) through the same
runner (`eval.runners.harness.run_eval`). Both run on demand only: locally
with `make` targets, and in CI through a manually triggered workflow. A
failing or skipped dimension fails the run, and only a passing report is
promoted to a baseline.

![Two evaluation harnesses fed by each reasoner's EvalAdapter. The framework harness runs consistency and robustness through the reasoner's own pipeline (its EvalDriver, submitting through LocalClient) and runs shadow evaluation, which samples the run's recorded bundles and re-runs the advisory gates offline. The RAG policy gate's harness runs retrieval against the gate's hybrid retriever, and faithfulness and citation with LLM judges. Each harness writes an EvalReport to its own staged path: outputs/stage/eval/reasoner/<reasoner_id>/ and outputs/stage/eval/gate/policy/<reasoner_id>/.](../assets/diagrams/eval-harness.svg)

---

## Why two harnesses

Standard ML evaluation (hold-out accuracy, AUC, F1) is necessary but not
sufficient for a governed decision system. It does not catch:

- retrieval surfacing the wrong policy evidence, upstream of everything else;
- a model producing confident rationale not grounded in what it retrieved;
- citations that are present but do not support their claims;
- the final action changing with the order events arrive in;
- correct behavior degrading under adversarial input.

The first three are properties of one gate, and only that gate can measure
them: they need its retriever, its prompts, and its output fields. The last
two are properties of the decision the ledger writes, whatever gates
produced it. So the gate's harness owns the first group and the framework
harness the second, and neither imports the other's internals.

---

## The framework harness

The harness is reasoner-agnostic and evaluates one reasoner per run.
Everything reasoner-specific comes from the reasoner's `EvalAdapter`
(`eval/adapter.py`), published under the `decisionledger.eval_adapters`
entry-point group with the reasoner's `reasoner_id` as its name:

| Adapter member | Used for |
|---|---|
| `reasoner_id` | The reasoner evaluated; must equal the entry-point name. |
| `event_type` | The Pydantic model the datasets' events are validated against. |
| `datasets_dir` | Root of the reasoner's golden datasets: `scenarios/` and `robustness/` for this harness, plus whatever a gate's harness reads. |
| `build_driver(settings)` | An `EvalDriver` into the reasoner's live pipeline, whose `framework` services (the bundle store, the composed gates, the registry) back the shadow dimension. |

Both shipped reasoners publish one: ATO's (`ato_reasoner.eval.adapter:ADAPTER`)
and the example reasoner's (`example_reasoner.eval.adapter:ADAPTER`).

```bash
make eval-account_takeover     # the ATO reasoner
make eval-example              # the example reasoner
uv run python -m eval.runners.harness --reasoner-id <reasoner_id>   # any installed reasoner
uv run python -m eval.runners.harness --reasoner-id <reasoner_id> --output /tmp/eval-report.json
```

The reasoner is named with `--reasoner-id` or `DECISIONLEDGER_EVAL_REASONER`.
The report goes to `--output`, else `EVAL_OUTPUT_PATH`, else
`outputs/stage/eval/reasoner/<reasoner_id>/eval-report.json`. Consistency and
robustness run concurrently; shadow runs after them, because it samples what
they recorded. A dimension whose dataset is missing is reported as skipped,
and a skipped dimension fails the run. Exit codes: `0` when every dimension
passed, `1` when one failed, `2` for a configuration error (no reasoner named,
no adapter installed under that id, or a driver configuration error such as a
missing credential the reasoner's pipeline needs), in which case no report is
written. The harness checks no API key itself.

### Consistency

**What it measures.** Given the same events in different orders, does the
ledger reach the same final action on the trigger event?

**Why it matters.** A system that allows a login when its precursor events
arrive in one order and blocks it when they arrive in another is not safe to
deploy, however it performs on average. The property depends on the bundle's
immutable `decision_action`; standard RAG evaluation has no equivalent.

**Dataset.** `scenarios/*.yaml`: each scenario has at least two orderings of
the same events, and the last event of each ordering is the trigger. ATO ships
four scenarios with three orderings each (chronological, precursors reversed,
shuffled);
the example reasoner ships one file of recorded scenarios.

| Metric | Threshold |
|---|---|
| `action_stability_rate`: fraction of scenarios whose orderings all agree on the trigger's action | = 1.0 (zero tolerance) |
| `confidence_variance` across orderings | telemetry, not gated |
| `rationale_semantic_similarity` | telemetry, not gated; a placeholder that always reports 1.0 |

### Robustness

**What it measures.** Does the ledger stay safe under injection attempts,
gate failures, and event patterns outside the scorer's training
distribution?

**Dataset.** Four files under `robustness/`, each optional: `injection.yaml`,
`malformed.yaml`, `novel.yaml`, `fallback.yaml`. The dimension is skipped only
when all four are missing.

| Sub-check | What the driver does | Threshold |
|---|---|---|
| `injection_resistance_rate` | Runs a clean event and the same event with injection text in its user-controllable fields; the actions must match. | = 1.0 |
| `schema_violation_handling` | Forces the gate's output to have no verdict; the action must be the expected one (HOLD, through the schema-failure tier). | = 1.0 |
| `novel_pattern_action_accuracy` | Runs adversarial patterns (after any priming events); the action must be conservative (HOLD or BLOCK). | ≥ 0.90 |
| `fallback_behavior_correctness` | Forces a retrieval, corpus, or model failure; the action must be the expected fallback. | = 1.0 |

A sub-check whose file is missing scores 1.0. The schema-violation and
fallback cases are deterministic; the injection and novel-pattern cases run
the live pipeline. The example reasoner ships `malformed.yaml` and
`fallback.yaml` only; its driver forces every fallback by swapping in a gate
that raises, which the ledger fails closed.

### Shadow

**What it measures.** A recorded decision (mode `record`, citing a
registered bypass rule) skips its advisory gates
([DR-30](decisions.md#dr-30-record-and-gate-the-two-submission-modes)).
Shadow evaluation asks what the ledger would have decided had the reasoner
asked for a gate instead. For each sampled recorded bundle it turns the
stored submission into a gate request with no proposal, runs the advisory
gates on it with their stored gate contexts through the same fail-closed
runner live decisions use (`app.submit.run_gate_set`), keeps the stored
mandatory gate outcomes, and applies the decision-level rules and the
combinator as in gate mode, novel-entity tier included. Nothing is written to
the ledger.

**Sample.** The ten most recent recorded decisions
(`DEFAULT_SHADOW_SAMPLE_SIZE`) of this reasoner written since the run
started, so it shadows what this run recorded, not the ledger's history.

| Metric | Meaning |
|---|---|
| `agreement_rate` | The hypothetical action equals the recorded one. |
| `escalation_rate` | The gates would have been stricter: what the bypass rule let through. |
| `relaxation_rate` | The gates would have been more lenient. |
| `gate_failure_rate` | Shadow gate invocations that failed closed. |

The dimension is **report-only**: it passes whenever it runs, until a
baseline calibrates a threshold. Escalations are expected for ATO: its
recorded decisions about novel accounts would be held by the novel-entity
tier if gated. Each sampled decision costs one call per advisory gate, which
for the RAG policy gate is one paid model call.

---

## The RAG policy gate's harness

The gate's harness (`gates/policy_rag/src/policy_rag_gate/eval/`) runs its
three dimensions against the datasets a reasoner ships for the gate, read
from that reasoner's `EvalAdapter.datasets_dir`:

| Dataset | Dimension |
|---|---|
| `retrieval/golden_queries.yaml` | Retrieval, through the gate's own retrieval over the reasoner's binding (every corpus it names, through its provider, merged as the gate renders it), with report-only per-corpus metrics (`per_corpus.<corpus_id>.<metric>`: precision at the quota, mean reciprocal rank, hit rate, passages returned, reranker bypass rate) |
| `faithfulness/golden_outputs.yaml` | Faithfulness (RAGAS and an LLM judge) |
| `citations/golden_outputs.yaml` | Citation (LLM judges) |
| `authority/golden_outputs.yaml` | Optional (DR-34): captured gate outputs of a template with tiered evidence; the citation dimension reports, report-only, the authority-violation rate — verdicts naming a lower-tier corpus as governing while a higher-tier passage on point was retrieved |

```bash
make eval-gate-policy_rag                    # every installed reasoner whose adapter ships one of these datasets
make eval-gate-policy_rag REASONER=<id>      # one reasoner
uv run python -m policy_rag_gate.eval [--reasoner-id <id>] [--output-dir <dir>]
```

Without a reasoner id (or `DECISIONLEDGER_EVAL_REASONER`), it evaluates every
installed adapter that ships at least one of the three datasets; today that
is ATO only. It writes one report per reasoner, to
`outputs/stage/eval/gate/policy/<reasoner_id>/eval-report.json` by default.
A missing dataset is a skipped dimension, which fails that report. It needs
`OPENAI_API_KEY` (RAGAS) and `ANTHROPIC_API_KEY` (the judges), and exits `2`
without writing a report when either is missing, when no installed adapter
ships a dataset, when the named reasoner has no adapter, or when the
ledger's registrations do not compose. The retrieval dimension reads the
reasoner's binding from the registry the ledger composes (run it with the
ledger's `DECISIONLEDGER_REASONERS` and `DECISIONLEDGER_REGISTRATIONS`;
a reasoner that does not bind the gate to a corpus skips it) and needs the
binding's corpora loaded (`make build-policy-index-<reasoner_id>`). Exit `0` means every report
passed, `1` that a dimension failed or was skipped.

The judges run on Anthropic (`claude-sonnet-4-6` by default) and RAGAS on
OpenAI (`gpt-4o-2024-08-06`), while the gate itself calls OpenAI: the judges
are a different model family from the gate, to reduce in-family bias
([DR-22](decisions.md#dr-22-eval-harness-ragas-in-openevals-out-custom-sdk-agnostic-llm-judge-primitive),
[DR-23](decisions.md#dr-23-unified-llmclient-adapter-one-provider-boundary-for-gate-and-judge)).

### Retrieval

**What it measures.** Does the retriever surface the right policy evidence
for each query, including the current version of a superseded document and
only the jurisdictions a query allows?

**Why it matters.** If the wrong evidence reaches the model, no reasoning
quality fixes the output, and the model writes confident rationale citing
whatever it received.

Classical IR formulas over annotated relevant documents, no LLM. ATO's golden
set has twelve queries.

| Metric | Threshold |
|---|---|
| `context_precision_at_k` (k = 5) | ≥ 0.40 |
| `context_recall_at_k` (k = 5) | ≥ 0.75 |
| `mean_reciprocal_rank` | ≥ 0.70 |
| `version_resolution_accuracy`: the expected document and version appear in the top three | = 1.0 |
| `jurisdiction_filter_accuracy`: every snippet lies within the query's jurisdictions | = 1.0 |

The precision floor is 0.40 rather than 0.80 because the golden queries have
one or two relevant documents each, which caps precision at five well below
0.80 even with perfect ranking; recall and MRR are the primary signal.
Version resolution checks the top three rather than the top result
([DR-25](decisions.md#dr-25-mvp-eval-calibration-bimodal-scorer-thresholds-top-k-version-resolution-realistic-faithfulness-floor)).

These thresholds were calibrated on a binding that names one corpus, at
k = 5. The overall metrics score the passages in the order the gate renders
them, at k = `k_per_corpus` × the corpora the binding names, so a binding
that adds a corpus needs thresholds of its own (the report-only per-corpus
metrics are there to calibrate them); every binding today names one corpus
([DR-34](decisions.md#dr-34-retrieval-providers-corpus-references-the-evidence-contract-and-evidence-escrow)).

### Faithfulness

**What it measures.** Does the gate's rationale say only what its retrieved
evidence supports?

**Why it matters.** A plausible rationale that cites real documents but
claims what they do not say is invisible to enforcement and to a reviewer
who does not check every citation.

| Metric | Threshold |
|---|---|
| `ragas_faithfulness`: RAGAS decomposes the rationale into claims and checks each against the contexts | ≥ 0.65 |
| `llm_judge_grounding`: an independent judge prompt scores grounding | ≥ 0.80 |
| `citation_rationale_overlap`: word-level Jaccard between rationale and cited text | ≥ 0.40 |
| `hallucination_rate`: cases whose judge grounding is below 0.5 | = 0.0 |

The RAGAS floor of 0.65 is calibrated to the current hand-crafted dataset:
RAGAS' atomic-claim check is strict against rationale whose claims are not
verbatim in the contexts. The aspirational target is 0.85, once the dataset
is dominated by live captures from a grounding-disciplined prompt
([DR-25](decisions.md#dr-25-mvp-eval-calibration-bimodal-scorer-thresholds-top-k-version-resolution-realistic-faithfulness-floor)).
ATO re-captures its faithfulness fixtures from live gate runs with
`make capture-baselines-account_takeover`.

### Citation

**What it measures.** Does each cited snippet support the claim it is cited
for, or is the citation present but irrelevant?

| Metric | Threshold |
|---|---|
| `citation_relevance_score`: a judge rates support 1 to 5, normalized to [0, 1] | mean ≥ 0.80 |
| `claim_citation_entailment`: a judge answers whether the snippet entails the claim | mean ≥ 0.70 |
| `superficial_citation_rate`: citations with relevance ≤ 0.4 (2 of 5) | ≤ 0.05 |

---

## What costs money

| Run | Paid calls |
|---|---|
| `make eval-account_takeover` | ATO's pipeline asks the policy gate for every event its bypass rules do not cover, so live cases call the gate's model (its driver requires `OPENAI_API_KEY`), and shadow evaluation calls it once per sampled record. |
| `make eval-example` | The example's scenarios are recorded (no gate) and its robustness cases are forced failures, but shadow evaluation re-decides its recorded decisions through the RAG policy gate's model. |
| `make eval-gate-policy_rag` | RAGAS (OpenAI) and the judges (Anthropic) on every faithfulness and citation case. Retrieval calls no model. |
| `make eval` | Every reasoner's harness, then every gate's. |

There is no sampling flag beyond the shadow dimension's sample size: a local
run costs the same as a CI run. Mark any test that calls a model (as a judge
or as the system under test) with `@pytest.mark.evaluation`, which keeps it
out of `make test`.

---

## Reports and baselines

| Path | What | Tracked |
|---|---|---|
| `outputs/stage/eval/reasoner/<reasoner_id>/eval-report.json` | The framework harness's latest report for a reasoner | no (`outputs/stage/` is gitignored) |
| `outputs/stage/eval/gate/policy/<reasoner_id>/eval-report.json` | The RAG gate harness's latest report for a reasoner | no |
| `outputs/eval/<kind>/<id>/eval-report-v<N>.json` | A promoted baseline: `<kind>` is `reasoner` (id: `reasoner_id`) or `gate` (id: `<gate_id>/<reasoner_id>`) | yes |
| `outputs/eval/eval-report-v<N>.json` | Baselines from before the split | yes |

Promote a baseline by copying a passing staged report to the next version
under `outputs/eval/<kind>/...`. No baseline has been promoted under the
per-kind layout yet. The two tracked reports predate the split, when one
harness ran all five dimensions for ATO: `eval-report-v1.json` (2026-05-06)
and `eval-report-v2.json` (2026-10-11, the run recorded when DR-26 landed).
Every dimension passed in both. The shadow dimension appears in neither.

An `EvalReport` holds `run_id`, `created_at`, `overall_passed` (true only when
every dimension passed), `dimensions` (one `DimensionResult` each: pass or
fail, metrics, threshold violations, sample count, timestamp), and one typed
metrics block per dimension (`retrieval`, `faithfulness`, `consistency`,
`citation`, `robustness`, `shadow`), `null` for a dimension that did not run.

---

## CI

| Workflow | Trigger | What it runs |
|---|---|---|
| `.github/workflows/ci.yaml` | Push to `main`, PR to `main` | Ruff format and lint, SQLFluff, Pyright, unit tests (no integration, evaluation, slow, or smoke tests), and the DR-23 SDK-import check |
| `.github/workflows/integration.yaml` | Push to `main`, PR to `main` | Per (reasoner, gate) pair: the framework DDL, `make prepare-gate-<gate>`, `make prepare-<reasoner>`, then `make test-integration` and `make test-smoke` against service containers (pgvector Postgres, Elasticsearch, Redis) |
| `.github/workflows/eval.yaml` | `workflow_dispatch` only | Per (reasoner, gate) pair, below |

The eval workflow runs in the `eval` GitHub Actions environment (its own
secrets, `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`), with a 30-minute timeout.
Its matrix is `reasoner: [account_takeover]` × `gate: [policy_rag]`, with
`account_takeover` mapped to `reasoner_id` `ato-reasoner`. Each job starts
Postgres (pgvector), Elasticsearch, and Redis, applies the framework DDL,
runs `make prepare-gate-policy_rag` and `make prepare-account_takeover`, then
`make eval-account_takeover`, then
`make eval-gate-policy_rag REASONER=ato-reasoner`. The gate harness runs even
when the reasoner harness failed, so both reports land, but never after a
preparation step failed. The job uploads `outputs/stage/eval/` as an
artifact kept for 30 days. The example reasoner is not in the matrix.

Quality and integration checks gate pull requests; the eval workflow
produces the reports a baseline is promoted from.

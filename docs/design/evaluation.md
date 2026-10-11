# Evaluation

The 5D evaluation framework that governs every release candidate. The harness
runs on demand (locally with `make eval`, in CI via `workflow_dispatch`; there
is no scheduled run); quality and integration gates run on every push to
`main` and every PR. A failing dimension blocks promotion of the eval baseline.

The harness is reasoner-agnostic and evaluates one reasoner per run. The
framework (`eval/`) owns the five dimensions, thresholds, and report format.
Each reasoner supplies an `EvalAdapter` (`eval/adapter.py`) through the
`decisionledger.eval_adapters` entry-point group: its event type, its golden
datasets, and an in-process driver into its decision pipeline. For ATO that
is `ato_reasoner.eval.adapter:ADAPTER`, with the driver in
`ato_reasoner/eval/driver.py` and the datasets in
`ato_reasoner/eval/datasets/`.

![Evaluation Harness — 5D Governance Gate](../assets/diagrams/eval-harness.svg)

---

## Why This Framework Exists

Standard ML evaluation — hold-out accuracy, AUC, F1 — is necessary but not
sufficient for a governed AI decision system. It doesn't catch:

- The retrieval layer surfacing the wrong policy evidence (upstream of
  everything else)
- The LLM generating confident, plausible rationale not grounded in retrieved
  content
- Action instability across equivalent event orderings
- Citations that are present but substantively irrelevant
- Correct behavior degrading under adversarial input

Each of these failure modes is invisible to a standard metric dashboard. Each is
catastrophic in a system where decisions carry regulatory consequence. The
five-dimension framework is designed specifically to catch them.

---

## Dimension 01 — Retrieval Quality

**What it measures:** Does the retriever surface the right policy evidence for
each decision context — including edge cases requiring version conflict
resolution and jurisdiction disambiguation?

**Why it matters:** The retrieval layer is the foundation. If the wrong policy
evidence reaches the LLM, no amount of reasoning quality fixes the output.
Retrieval errors compound silently — the LLM will generate confident,
well-formatted rationale citing whatever it received.

### Metrics

| Metric                       | Description                                                                   |
|------------------------------|-------------------------------------------------------------------------------|
| Context Precision @k         | Fraction of retrieved chunks that are relevant to the query                   |
| Context Recall @k            | Fraction of relevant chunks that are retrieved                                |
| Mean Reciprocal Rank         | How highly the first relevant result is ranked                                |
| Version Resolution Accuracy  | Rate at which the latest policy version is preferred over superseded versions |
| Jurisdiction Filter Accuracy | Rate at which jurisdiction-scoped queries return correctly scoped results     |

### Measurement

Evaluated against a golden query set of 12 curated queries, each with annotated
relevant policy chunks. The golden set covers:

- Direct regulatory queries ("NIST AAL2 requirements for authentication")
- Version-ambiguous queries (where v1.0 and v2.1 give different guidance)
- Jurisdiction-scoped queries (US federal vs. EU GDPR variants)
- Risk-tier-specific queries (standard account vs. high-value account)

### CI Gate Thresholds

```
Context Precision @5  ≥ 0.80
Context Recall @5     ≥ 0.75
MRR                   ≥ 0.70
Version Resolution    = 1.0   (zero tolerance — must always prefer latest)
Jurisdiction Filter   = 1.0   (zero tolerance — must never cross jurisdictions)
```

---

## Dimension 02 — Generation Faithfulness

**What it measures:** Does the LLM's rationale reflect only retrieved policy
content — or does it introduce claims not supported by the retrieved evidence?

**Why it matters:** The most dangerous failure mode in a policy-grounded system.
The LLM may generate a confident, plausible rationale citing real policy
documents but making claims those documents don't actually support. This is
invisible to the enforcement layer and invisible to a reviewer who doesn't
cross-check every citation. Faithfulness evaluation catches it before
production.

### Metrics

| Metric                       | Description                                                                             |
|------------------------------|-----------------------------------------------------------------------------------------|
| RAGAS Faithfulness Score     | Measures claim-level grounding against retrieved context                                |
| LLM-as-Judge Grounding Score | Secondary LLM evaluates whether each rationale claim is supported by the cited snippets |
| Citation-Rationale Overlap   | Text overlap between rationale claims and cited snippets                                |
| Hallucination Rate           | Binary: does the rationale contain any claim not traceable to retrieved content?        |

### Methodology Notes

RAGAS faithfulness decomposes the rationale into atomic claims and checks each
against the retrieved context. This is more reliable than whole-rationale
scoring but has its own failure modes — document in the harness code.

LLM-as-judge scoring introduces its own bias: the judge model may be more
charitable to outputs from the same model family. Use a different model family
for the judge where possible, and document the bias explicitly.

### CI Gate Thresholds

```
RAGAS Faithfulness    ≥ 0.65 (MVP floor; see DR-25)
LLM Judge Grounding   ≥ 0.80
Citation Overlap      ≥ 0.40
Hallucination Rate    = 0.0 on golden set
                      (any hallucination on the golden scenario set is a blocking failure)
```

The 0.65 RAGAS floor is calibrated against the current
hand-crafted-seed dataset; RAGAS' atomic-claim verification is
strict against any rationale whose claims are not verbatim in the
contexts. The aspirational target is 0.85, achievable once the
dataset is dominated by live captures with a grounding-disciplined
gate (a polish-phase prompt-engineering item). DR-25 in
[`decisions.md`](./decisions.md) records the calibration choice.

---

## Dimension 03 — Decision Consistency

**What it measures:** Given equivalent risk signals presented in different event
orderings, does the system produce the same final action?

**Why it matters:** LLMs are sensitive to input ordering. A decision system that
produces `ALLOW` when events are presented in one order and `BLOCK` when the
same events are in a different order is not safe for deployment, regardless of
how it performs on average. This dimension is specific to decision systems — it
doesn't exist in standard RAG evaluation frameworks.

### Test Design

8 named scenarios × 3 event orderings = 24 consistency tests.

For each scenario, three orderings are evaluated:

1. **Canonical:** Events in chronological order
2. **Reversed:** Events in reverse chronological order
3. **Shuffled:** Events in random order (seeded for reproducibility)

The same risk signals are present in all three orderings. The final action must
be identical across all three.

### Metrics

| Metric                        | Description                                                                   |
|-------------------------------|-------------------------------------------------------------------------------|
| Action Stability Rate         | Fraction of scenario × ordering combinations where action is consistent       |
| Confidence Variance           | Variance in reported confidence across orderings (informational — not a gate) |
| Rationale Semantic Similarity | Embedding similarity between rationale texts across orderings (informational) |

### CI Gate Thresholds

```
Action Stability Rate = 1.0 on all 24 tests
                        (any action instability is a blocking failure)
Rationale variance    — acceptable; not gated
Confidence variance   — tracked; not gated
```

---

## Dimension 04 — Citation Accuracy

**What it measures:** Do the cited policy snippets actually support the specific
claims they are attached to — or are citations present but substantively
irrelevant?

**Why it matters:** A system can satisfy grounding requirements on paper by
citing policy documents in every output while those citations don't actually
support the specific rationale claims. A compliance officer or auditor
evaluating decisions would flag this immediately. Citation accuracy evaluates
whether each citation is doing real work.

### Metrics

| Metric                    | Description                                                                      |
|---------------------------|----------------------------------------------------------------------------------|
| Citation Relevance Score  | LLM-as-judge rates how well each cited snippet supports the claim it's cited for |
| Claim-Citation Entailment | Does the cited text logically entail the attached rationale claim?               |
| Superficial Citation Rate | Fraction of citations rated as present-but-irrelevant by judge                   |

### Measurement

Evaluated on a golden output set (policy gate outputs generated from the 8
canonical scenarios, manually reviewed). For each citation in each output, the
LLM judge rates the citation on a 1–5 scale for claim support. Score ≥ 4 is "
relevant"; score ≤ 2 is "superficial."

### CI Gate Thresholds

```
Citation Relevance Score (mean)  ≥ 0.80
Superficial Citation Rate        ≤ 0.05
```

---

## Dimension 05 — Adversarial Robustness

**What it measures:** Does the system maintain correct behavior under injection
attempts, schema violations, and event patterns designed to confuse the scoring
and reasoning layers?

**Why it matters:** ATO attackers are adversarial by definition. They will probe
the system's behavior, attempt to inject into metadata fields, and construct
events designed to confuse or manipulate the reasoning layer. This dimension
ensures that the system's defenses hold under explicit adversarial pressure.

### Test Scenarios

| Scenario                                  | Attack Type                            | Expected Behavior                                            |
|-------------------------------------------|----------------------------------------|--------------------------------------------------------------|
| Injection in `user_agent`                 | Prompt injection attempt               | Sanitized before prompt construction; action unaffected      |
| Injection in `geo.city`                   | Prompt injection attempt               | Sanitized; `BLOCK` for adversarial flag                      |
| Malformed `device_fingerprint`            | Schema violation                       | Rejected at ingestion; `HOLD`                                |
| Truncated event payload                   | Missing required fields                | Rejected at schema validation; `HOLD`                        |
| Impossible field values                   | Semantic violation (negative velocity) | Rejected at feature validation                               |
| Novel entity + adversarial signal         | Combination attack                     | `HOLD` via novel entity trigger                              |
| Schema-valid but semantically adversarial | Plausible-but-crafted signals          | Scored correctly; not bypassed by crafting                   |
| `scenario_tag` present at scoring time    | Data leakage test                      | Field stripped before scoring; scorer performance unaffected |

### Metrics

| Metric                        | Description                                                                  |
|-------------------------------|------------------------------------------------------------------------------|
| Injection Resistance Rate     | Fraction of injection attempts that do not influence the final action        |
| Schema Violation Handling     | Fraction of malformed events correctly routed to `HOLD`                      |
| Novel Pattern Action Accuracy | Fraction of novel adversarial patterns routed to correct conservative action |
| Fallback Behavior Correctness | Fraction of error scenarios that correctly activate the fallback path        |

### CI Gate Thresholds

```
Injection Resistance Rate    = 1.0   (zero tolerance)
Schema Violation Handling    = 1.0   (zero tolerance)
Novel Pattern Accuracy       ≥ 0.90  (across 8 adversarial scenario variants)
Fallback Behavior            = 1.0   (zero tolerance — every error must route correctly)
```

---

## Running the Evaluation Harness

```bash
# Full eval gate (all 5 dimensions) for every installed reasoner
make eval

# One reasoner
make eval-account_takeover

# Equivalent direct invocation (--reasoner-id, or DECISIONLEDGER_EVAL_REASONER)
uv run python -m eval.runners.harness --reasoner-id ato-reasoner

# Write the report to a custom path
uv run python -m eval.runners.harness --reasoner-id ato-reasoner --output /tmp/eval-report.json
```

The harness loads the named reasoner's adapter, builds the default dimension
set against that adapter's datasets (for ATO,
`reasoners/account_takeover/src/ato_reasoner/eval/datasets/`) and driver, and
writes a structured `EvalReport` JSON to `outputs/stage/eval/eval-report.json`.
Exit codes: `0` if every dimension cleared its threshold, `1` if any
dimension failed, `2` for config errors (no reasoner named, no adapter
installed under that id, missing API keys). The committed baseline lives at
`outputs/eval/eval-report-v1.json`.

The committed baseline predates DR-26. It was produced on 2026-05-06, before
the active ATO prompt moved to `ato-v3` (the system prompt relocated into the
template) and before the retrieval dimension passed `reasoner_id`, so it
measures `ato-v2` and an unfiltered retrieval path. `make eval` has not been
re-run since; `ato-v3` activation is pending that run.

---

## CI Integration

CI is split across three workflows. The 5D harness is **not** invoked on every
push — it costs money and depends on cross-family LLM API keys.

| Workflow                                       | Trigger                                | What it runs                                                          |
|------------------------------------------------|----------------------------------------|-----------------------------------------------------------------------|
| `.github/workflows/ci.yaml`                    | Push to `main`, PR to `main`           | Lint (Ruff + sqlfluff) → typecheck (pyright) → unit tests → DR-23 boundary check |
| `.github/workflows/integration.yaml`           | Push to `main`, PR to `main`           | Per reasoner (`matrix.reasoner`): `make prepare-<reasoner>`, then `make test-integration` + `make test-smoke` against service containers (pgvector, Elasticsearch, plus the reasoner's Redis) |
| `.github/workflows/eval.yaml`                  | `workflow_dispatch` only               | Per reasoner: `make prepare-<reasoner>`, then `make eval-<reasoner>` against the live stack; uploads the report as an artifact |

Quality and integration gates block PR merges. The eval harness produces the
governance baseline that promotes a release candidate.

### Cost Management

Dimensions 02, 03, and 04 invoke LLM API calls as part of evaluation (
LLM-as-judge). These cost money. The harness runs against the full golden set in
CI. There is no sampling flag yet; a development run costs the same as a CI
run.

Mark any test that invokes the LLM (as judge or as the system under test) with
`@pytest.mark.evaluation` to exclude from the standard `make test` run.

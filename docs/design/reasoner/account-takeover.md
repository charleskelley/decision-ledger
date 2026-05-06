# Account Takeover Reasoner

The ATO Reasoner is the reference implementation of the
[reasoner pattern](./reasoner.md) for DecisionLedger. It demonstrates the
governed-decision architecture end-to-end on a single domain — authentication
events — while keeping the implementation small enough to read in a sitting.

For the abstract reasoner contract, see [`reasoner.md`](./reasoner.md). For the
typed handoff specification, see [`reasoner-handoff.md`](./reasoner-handoff.md).
For the training-data calibration provenance, see [`scenarios.md`](../scenarios.md).

---

## Goal

A minimal-surface ML decision system showing the four canonical layers of
governed decisioning end-to-end:

1. **Ingestion** — typed events with deduplication.
2. **Features** — online sliding-window computation.
3. **Scoring + routing** — fast ML triage that decides whether the event needs
   the policy gate.
4. **Handoff** — typed `Observation` submitted to the framework, which owns
   retrieval, gate evaluation, enforcement, and audit-bundle construction.

The architecture is the point. The data-source quality, scorer accuracy, and
policy-corpus depth are all calibrated for an MVP demonstration; production
deployments would replace each with operational artifacts.

---

## Components

| Layer | Module | Responsibility |
|---|---|---|
| Ingestion | `reasoner/account_takeover/ingestion/` | Redis Streams consumer + SHA-256 dedup; produces typed `LoginEvent`s. |
| Features | `reasoner/account_takeover/feature_service.py` | Redis sliding-window velocity + novelty + impossible-travel; produces `AtoFeatureVector`. |
| Scorer | `reasoner/account_takeover/scorer/` | XGBoost binary-logistic with TreeSHAP attribution; produces `ScorerOutput` (risk + signals + route). |
| Assembler | `reasoner/account_takeover/assembler.py` | Composes event + features + scorer output into a typed `Observation` with populated `ReasonerContext` and `GateContext`. |
| API | `app/main.py` | FastAPI surface; orchestrates the pipeline per request. |

The boundaries are intentional. `reasoner/account_takeover/scorer/` knows about XGBoost; `core/`
doesn't. `reasoner/account_takeover/` knows about login events;
`core/observation/` doesn't. Adding a different reasoner means following
this layering, not modifying the framework.

---

## Hybrid fast / gate routing

The scorer produces a risk probability in `[0, 1]`. The routing decision uses
two confidence-band thresholds:

| Risk score band | Route | Pipeline behavior |
|---|---|---|
| `< 0.20` | `FAST_PATH_ALLOW` | Skip retrieval + gate; enforcement applies permissive default. |
| `0.20 – 0.95` | `ROUTE_TO_GATE` | Run retrieval + LLM policy gate; enforcement consumes verdict. |
| `> 0.95` | `FAST_PATH_BLOCK` | Skip retrieval + gate; enforcement applies blocking default. |

The thresholds live as public constants in `reasoner/account_takeover/scorer/scorer.py`
(`FAST_PATH_ALLOW_THRESHOLD`, `FAST_PATH_BLOCK_THRESHOLD`) and are recorded
in the `ModelCard` sidecar at training time so the routing distribution
captured against the test set is interpretable later.

### Why hybrid

Pure ML triage is fast (<10ms inference for the XGBoost scorer) but cannot
explain itself in policy terms — a regulatory reviewer asking "why was this
denied" gets a SHAP plot, not a citation to a regulation. Pure LLM triage
reasons in policy terms with retrievable citations but is slow (seconds) and
expensive (tokens-per-decision). Hybrid routing applies each where it pays
for itself: ML on confident events (the bulk of the volume), LLM on
ambiguous ones (the cases where reasoning matters).

### Shadow evaluation

Even on fast-path routes, the assembler populates `GateContext` on the
`Observation`. The framework can retrospectively run the gate offline
against a fast-path bundle to compare what the gate *would* have said —
shadow evaluation. Without `GateContext` on fast-path, that capability is
forfeit. See [`reasoner-handoff.md`](./reasoner-handoff.md) for the
contract specification.

---

## Training pipeline

### Data

Synthetic feature rows from five archetype distributions inside
`reasoner/account_takeover/scorer/trainer.py:_generate_sample()`:

| Archetype | Sampled prevalence | Pattern |
|---|---|---|
| Normal / low-risk | 55% | Low velocity, established device, no novelty |
| High-velocity / credential stuffing | 15% | Velocity spikes, IP novelty, low consistency |
| Impossible travel | 10% | Geographic anomaly, high travel-speed |
| Novel entity / sparse history | 10% | New account, partial signals across the board |
| Mixed / ambiguous | 10% | Random combinations meant to land in the gate band |

The training set is generated in-memory from these archetypes; the exact
train and test partitions are persisted as CSV alongside the committed model
(`reasoner/account_takeover/scorer/models/ato-v1.{train,test}.csv`) for reproducibility and
inspection.

The archetype distributions are *qualitatively informed* by published
descriptive statistics from Wiefling et al. (ACM TOPS 2022); see
[`scenarios.md`](../scenarios.md) for the citation-by-citation mapping and
the polish-phase plan to do raw RBA-dataset analysis.

### Labels

Heuristic labeling function in `_heuristic_label()` — additive risk score
combining velocity, novelty, travel, and consistency signals; thresholded
at 0.5 for the binary label. The label function is committed, documented,
and version-tagged (`HEURISTIC_LABEL_VERSION`). Per DR-10, this is
explicitly framed as a triage-classifier label, not a fraud-detector
label. The scorer is not claiming to detect real fraud; it's deciding
which events need the LLM gate's attention.

### Train / test split + evaluation

`train()` in `reasoner/account_takeover/scorer/trainer.py`:

1. Generates the dataset (default: 5000 samples).
2. Deterministic 80/20 split (numpy permutation, seed 42).
3. Fits XGBoost (binary:logistic, 100 estimators, max_depth=4).
4. Computes train + test metrics: AUC, log-loss, precision/recall at 0.5,
   confusion matrix, fast-path routing distribution.
5. Captures `git_sha` of the training commit.
6. Writes the binary `.ubj`, the JSON `ModelCard` sidecar, and the
   train/test CSVs.

Run:

```bash
make train       # 5000 samples → reasoner/account_takeover/scorer/models/ato-v1.{ubj,json,train.csv,test.csv}
make eval-model  # Re-evaluate on a fresh-seed test set; exits non-zero if test_auc < 0.85
```

### Model card and integrity

The `ModelCard` (in `reasoner/account_takeover/scorer/model_card.py`) is the persisted contract
between training and runtime. At load time, `AtoScorer.__init__`
validates two integrity properties:

- `feature_names` in the card matches `FEATURE_NAMES` in
  `reasoner/account_takeover/scorer/scorer.py`. Mismatch → `ValueError` (catches schema drift
  between training and inference).
- `artifact_sha256` in the card matches the binary on disk. Mismatch →
  `ValueError` (catches post-write tampering).

A legacy artifact without a sidecar still loads with a warning, so the
change is backward-compatible with cached test fixtures.

---

## Decisions & Tradeoffs

The MVP is deliberately scoped to be a *reference architecture demonstration*,
not a production fraud detector. Every choice below reflects that scope.

- **Synthetic training data over real-data integration.** In-scope is the
  *architecture pattern*: data → split → train → evaluate → version →
  deploy → integrity-check at load. The data-pipeline depth (real RBA
  ingestion, distribution fitting, validation) is a separate body of work
  scoped as a polish-phase follow-up in [`scenarios.md`](../scenarios.md).
  Cite DR-10.

- **Hardcoded fast-path thresholds (0.20 / 0.95) over per-deploy
  calibration.** Keeps the reference implementation reproducible and the
  routing distribution interpretable across runs. The model card captures
  the distribution these thresholds produce on the test set so a future
  reviewer can validate the choice without rerunning the pipeline.
  Threshold tuning is a production concern post-MVP. The 0.95 upper
  cutoff (rather than the original 0.85) reflects the binary-classifier
  scorer's bimodal output distribution — moderately-confident attacks
  (0.85–0.95) reach the LLM gate for adjudication rather than
  auto-blocking. See DR-25 for the calibration record.

- **Heuristic labels over hand-labeled data.** Explicitly framed as a
  triage-classifier label, not a fraud-detection label. The scorer's job
  is sorting events into confidence bands so ambiguous events route to
  the gate. Cite DR-10.

- **XGBoost over a neural network.** TreeSHAP gives free per-event
  attribution (no separate explainability layer); CPU inference fits the
  <10ms latency budget; the artifact is sub-MB which fits the
  "commit-the-baseline" decision (a reviewer can `git clone && docker
  compose up && uvicorn` and have a working pipeline). A neural network
  would need a separate explainability tool, GPU for low-latency
  inference, and a multi-MB artifact.

- **Sliding-window features in Redis over a feature store.** Redis is
  already in the stack for ingestion; adding a managed feature store for
  the reference implementation introduces infrastructure that doesn't
  pay for itself at MVP scope. A production deployment would likely
  replace this with Feast, Tecton, or a custom Kafka-backed store; the
  feature contract in `reasoner/account_takeover/feature_service.py` is small enough to swap.

- **Commit the model artifact (and train/test CSVs).** Sub-MB total; the
  cost is a binary in git. The benefit is `git clone && uvicorn` works
  out-of-the-box for a portfolio reviewer or CI. A production system
  would use a model registry; for an MVP demonstration the simpler path
  wins.

- **Card-aware loading is mandatory when a card exists; legacy load works
  without one.** Backward compatibility for any cached test fixtures
  that pre-date the card. New artifacts always carry a card; old ones
  load with a warning.

---

## Cross-references

- **[`reasoner.md`](reasoner.md)** — abstract reasoner typology and
  required information.
- **[`reasoner-handoff.md`](reasoner-handoff.md)** — typed handoff
  contract specification.
- **[`scenarios.md`](../scenarios.md)** — scenario-generator distribution
  calibration provenance and the polish-phase RBA-calibration plan.
- **DR-10** — hybrid data strategy.
- **DR-11** — `core/` / `reasoner/` package boundary.
- **DR-12** — `entity_id` as the universal subject identity primitive.
- **DR-14** — `Observation` as domain-assembled output.
- **DR-17** — model-artifact fingerprint approach.

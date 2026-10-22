# Account Takeover Reasoner

The ATO Reasoner is the reference implementation of the
[reasoner pattern](../index.md) for DecisionLedger. It shows the
governed-decision architecture end to end on one domain, authentication
events, while keeping the implementation small enough to read in a sitting.

For the abstract reasoner contract, see [Reasoners](../index.md). For the wire
contract every reasoner submits, see the
[Submission contract](../reasoner-handoff.md). For the training-data
calibration provenance, see [Scenarios](scenarios.md); for the data shapes,
see [Data Model](data.md).

The reasoner is a uv workspace member at `reasoners/account_takeover/`
(distribution `ato-reasoner`, import package `ato_reasoner`, `reasoner_id`
`ato-reasoner`). Module paths below are relative to
`reasoners/account_takeover/src/`. A ledger deployment discovers it through
the `decisionledger.reasoners` entry point (`ato_reasoner.plugin:PLUGIN`) and
the eval harness through `decisionledger.eval_adapters`
(`ato_reasoner.eval.adapter:ADAPTER`); nothing in `core/`, `app/`, or `eval/`
imports it.

Everything on this page is ATO's: the score bands, the rule ids
`allow-band` and `block-band`, the features, the prompt templates, the
jurisdictions, and the `ato-account` namespace live in ATO's package, not in
the framework.

---

## Goal

A minimal-surface ML decision system showing the layers of governed
decisioning end to end:

1. **Ingestion**: typed events with deduplication.
2. **Features**: online sliding-window computation.
3. **Scoring**: fast ML triage that produces a risk score and its
   attribution.
4. **Submission**: a `Submission` (`core.submission`, wire schema version 2)
   that either records the decision ATO made, citing one of its registered
   bypass rules, or asks the ledger to gate it. The ledger resolves the
   subject, runs the gates, enforces, and writes the Decision Bundle.

The architecture is the point. The data-source quality, scorer accuracy, and
policy-corpus depth are calibrated for an MVP demonstration; a production
deployment would replace each with operational artifacts.

---

## Components

| Layer | Module | Responsibility |
|---|---|---|
| Ingestion | `ato_reasoner/ingestion/` | Redis Streams consumer, lateness window, and SHA-256 dedup of the `event_id`; produces typed `LoginEvent`s and dead-letters the rest. `python -m ato_reasoner consume` decides each accepted login through the pipeline (`ingestion/stream.py`). |
| Features | `ato_reasoner/feature_service.py` | Redis sliding-window velocity, novelty, and impossible-travel signals; produces `AtoFeatureVector`. |
| Scorer | `ato_reasoner/scorer/` | XGBoost binary-logistic model with TreeSHAP attribution; produces `ScorerOutput` (risk score and top signals). |
| Assembler | `ato_reasoner/assembler.py` | `build_submission`: event, features, and score into one `Submission` with its `ReasonerContext`, the policy gate's `GateContext`, the native subject, and the mode. |
| Pipeline | `ato_reasoner/pipeline.py` | `run_ato_decision`: features, score, assemble, then submit through a `LedgerClient` and return the receipt with ATO's own step timings. |
| Service | `ato_reasoner/app.py`, `ato_reasoner/api.py` | ATO's own FastAPI app (`create_app`): `POST /api/v1/ato/decisions` and `GET /health`, on port 8001. |
| Settings | `ato_reasoner/settings.py` | `AtoSettings` (prefix `ATO_`): Redis URL, scorer artifact, the policy gate template (`ATO_TEMPLATE_ID`: `ato-v4` by default; `ato-v2` and `ato-v3` selectable), ledger URL and key. |
| Plugin | `ato_reasoner/plugin.py` | `PLUGIN`: the registration and the `prompts/` and `corpus/` directories the RAG policy gate reads; `build_ato_services` opens Redis and loads the scorer for ATO's own processes. |
| Registration | `ato_reasoner/registry.py` | `ATO_REGISTRATION`: the gate binding (its configuration names the corpus `ato-policy` at tier 1, DR-34), the bypass rules, the recordable actions, the native namespace, and the enforcement profile. |
| Prompts and corpus | `ato_reasoner/prompts/`, `ato_reasoner/corpus/` | Gate templates `ato-v1` to `ato-v4` (default: `ato-v4`, which renders the evidence by authority tier with the conflict/gap protocol; `ato-v3` reproduces the behavior before DR-34; `ato-v1` cannot be rendered again) and the 32 policy documents loaded as the corpus `ato-policy`. |
| Generator | `ato_reasoner/generator/` | The synthetic login-event generator and its eight scenarios ([Scenarios](scenarios.md)). |
| Eval | `ato_reasoner/eval/` | The `EvalAdapter`, the in-process driver, the baseline capture, and the datasets for the framework harness and the policy gate's harness. |
| Infrastructure | `reasoners/account_takeover/compose.yaml` | Redis, included into the root stack; configured with `ATO_REDIS_URL`. |

The boundaries are intentional. `ato_reasoner/scorer/` knows about XGBoost;
`core/` and `app/` do not, and the framework's dependencies do not include
it. `ato_reasoner/` knows about login events; the ledger sees a login only as
the opaque `raw_event` JSON of a submission. Adding a different reasoner
means following this layering, not modifying the framework.

---

## How ATO decides: record or gate

The scorer produces a risk probability in `[0, 1]`; the assembler sets it as
the `ReasonerContext.label_value` (`label_name` `risk_score`). Whether a login
is recorded or gated is not the scorer's call. The assembler checks ATO's
registered bypass rules with `core.submission.matching_bypass_rule`, which
returns the strictest rule that holds:

| Risk score | Bypass rule (version) | Submission | What the ledger runs |
|---|---|---|---|
| `< 0.20` | `allow-band` (`1`) | `mode: record`, `proposed_action: ALLOW`, citing `allow-band@1` | Mandatory gates only; the policy gate is recorded as skipped. |
| `0.20` to `0.95` | none | `mode: gate`, no proposal | Mandatory gates, then the advisory policy gate. |
| `> 0.95` | `block-band` (`1`) | `mode: record`, `proposed_action: BLOCK`, citing `block-band@1` | Mandatory gates only; the policy gate is recorded as skipped. |

The rules are `BypassRule`s in `ato_reasoner/registry.py`: a predicate in the
framework's predicate language (`Compare` on `label_value`, `lt 0.20` and
`gt 0.95`) and the one action it justifies. The thresholds are the constants
`ALLOW_BAND_THRESHOLD` and `BLOCK_BAND_THRESHOLD` in the same module; the
scorer imports them for its band metrics, and they appear nowhere in the
framework. ATO's `recordable_actions` are ALLOW and BLOCK, the bands' actions.

The registration binds the RAG policy gate (`gate_id` `policy`) as advisory:
it runs only when ATO asks for a gate. ATO binds no mandatory gate; a
deployment may mandate gates for every reasoner
(`DECISIONLEDGER_MANDATORY_GATES`), and those run on ATO's records too.

What the ledger does with each mode (DR-29, DR-30):

- **Record.** The ledger checks the citation (a registered rule, its action,
  its version), re-evaluates the cited rule and the other registered rule
  against the submission's evidence, and snapshots both checks in the
  bundle. A cited rule that does not hold puts a HOLD floor under the
  decision; it is not rejected. Tier 3 does not apply to records.
- **Gate.** The ledger runs the policy gate on the gate context ATO sent.
  The gate retrieves from ATO's indexed corpus, asks the LLM for a verdict,
  and returns a `GateOutput`; deterministic enforcement turns it into the
  final action. ATO's enforcement profile applies: Tier 3 fires on the
  `sparse_history` feature (a novel account's advisory verdicts are held for
  review), and Tier 5 treats US (`US_FEDERAL`, `US_STATE`) and EU
  (`EU_GDPR`) evidence in one retrieval set as a jurisdiction conflict. A
  gate that fails (an error, a timeout, an unusable output) fails closed to
  HOLD.

Every submission, recorded ones included, carries the policy gate's
`GateContext`: the template id (`ATO_TEMPLATE_ID`, `ato-v4` by default), its
template variables, the jurisdiction filter (`INTERNAL`, plus `US_FEDERAL`
for a US login), the risk tier (`HIGH_VALUE` at a score of 0.60 or more,
else `STANDARD`), and the retrieval query ATO renders
(`ato_reasoner/retrieval_query.py`). It names no corpus: ATO's registration
binds the gate to `ato-policy` at tier 1, and the gate retrieves from that
corpus itself, recording its state on every gated decision.

### Why record and gate

Pure ML triage is fast (the XGBoost scorer targets under 10 ms P95) but
cannot explain itself in policy terms: a regulatory reviewer asking why a
login was denied gets a SHAP plot, not a citation to a regulation. Pure LLM
triage reasons in policy terms with retrievable citations but is slow
(seconds) and costs tokens per decision. ATO applies each where it pays for
itself: it records the confident scores (the bulk of the volume) under rules
the ledger can check and replay, and asks the gate about the ambiguous ones.

### Shadow evaluation

Because every recorded submission carries the policy gate's context, a
recorded login can be re-decided by the gate later. That is the framework
harness's shadow dimension (`eval/dimensions/shadow.py`): it samples recorded
bundles, runs their advisory gates offline on the stored submission, and
compares the hypothetical action with the recorded one. It is report-only
today; escalations are expected for ATO, whose novel-account records Tier 3
would hold if they were gated. See [Evaluation](../../evaluation.md).

---

## The subject: an account

ATO names the subject it scores as its native subject:
`(ato-account, account_id)`. Its registration declares that namespace
(`ATO_ACCOUNT_NAMESPACE` in `ato_reasoner/registry.py`): subject type
`account`, keyed under the UUID5 namespace
`8a1f9b2e-4c3d-5e6f-7a8b-9c0d1e2f3a4b`. `LoginEvent.entity_id` is the
account's id under that namespace, so every ATO submission carries
`entity_id = uuid5(namespace, account_id)`, `entity_type` `account`, and the
native subject. Intake refuses a submission whose `entity_id` or
`entity_type` disagrees with the declaration (`422 NATIVE_SUBJECT_MISMATCH`).

The ledger resolves the subject before any gate runs (DR-32). With no
identity resolver active, the passthrough derives the subject from the
declared namespace, which is exactly the `entity_id` and `entity_type`
ATO's decisions always had, and records it `unresolved` with the reason. An
enterprise resolver may instead map the account to the subject it belongs
to (a customer, say); the bundle then records that subject and its
provenance, and the submission keeps ATO's own account id. Either way the
decisions are listed under the recorded subject's id by
`GET /api/v1/entities/{entity_id}/decisions`. See
[Identity](../../identity.md#the-passthrough).

---

## Running ATO

ATO runs as its own service; the ledger mounts no reasoner router (DR-31).

```bash
make serve-account_takeover   # uv run uvicorn --factory ato_reasoner.app:create_app --port 8001
```

`POST /api/v1/ato/decisions` takes a `LoginEvent` as JSON, scores it, submits
the `Submission`, and returns a summary of the ledger's receipt
(`decision_id`, `decision_action`, `mode`, `bypass_rule_id`,
`bypass_rule_holds`, `enforcement_rule_applied`, one entry per gate, and the
whole decision's `latency_ms`). The full bundle, replay, and the account's
timeline are the ledger's routes.

Which ledger ATO submits to is a setting:

- `ATO_LEDGER_URL` unset (the default): `LocalClient.from_settings()`
  composes the ledger inside ATO's own process, as the ledger service would
  (installed reasoner plugins, the registration file, installed gates), and
  writes to the same Postgres. ATO acts as the trusted in-process caller.
- `ATO_LEDGER_URL` set (for example `http://localhost:8000`, the ledger
  started with `make serve`): `HttpClient` calls that ledger with
  `ATO_LEDGER_KEY`, ATO's per-reasoner key, configured on the ledger as
  `DECISIONLEDGER_API_KEYS=ato-reasoner:<key>`.

A ledger refusal is passed through as the ledger's own problem document; an
unreachable ledger is a `503 LEDGER_UNAVAILABLE` problem.

---

## Training pipeline

### Data

Synthetic feature rows from five archetype distributions inside
`ato_reasoner/scorer/trainer.py:_generate_sample()`:

| Archetype | Sampled prevalence | Pattern |
|---|---|---|
| Normal / low-risk | 55% | Low velocity, established device, no novelty |
| High-velocity / credential stuffing | 15% | Velocity spikes, IP novelty, low consistency |
| Impossible travel | 10% | Geographic anomaly, high travel speed |
| Novel entity / sparse history | 10% | New account, partial signals across the board |
| Mixed / ambiguous | 10% | Random combinations meant to land between the bands |

The training set is generated in memory from these archetypes; the exact
train and test partitions are persisted as CSV alongside the committed model
(`ato_reasoner/scorer/models/ato-v1.{train,test}.csv`) for reproducibility and
inspection.

The archetype distributions are *qualitatively informed* by published
descriptive statistics from Wiefling et al. (ACM TOPS 2022); see
[Scenarios](scenarios.md) for the citation-by-citation mapping and the
polish-phase plan to do raw RBA-dataset analysis.

### Labels

A heuristic labeling function in `_heuristic_label()`: an additive risk score
combining velocity, novelty, travel, and consistency signals, thresholded at
0.5 for the binary label. The label function is committed, documented, and
version-tagged (`HEURISTIC_LABEL_VERSION`). Per DR-10, this is a
triage-classifier label, not a fraud-detector label. The scorer does not
claim to detect real fraud; it sorts logins into confidence bands so the
ambiguous ones reach the policy gate.

### Train / test split and evaluation

`train()` in `ato_reasoner/scorer/trainer.py`:

1. Generates the dataset (default: 5000 samples).
2. Splits it 80/20 deterministically (numpy permutation, seed 42).
3. Fits XGBoost (`binary:logistic`, 100 estimators, `max_depth=4`).
4. Computes train and test metrics: AUC, log-loss, precision and recall at
   0.5, the confusion matrix, and the test set's score-band distribution
   (`allow_band`, `gate`, `block_band`).
5. Captures the `git_sha` of the training commit.
6. Writes the binary `.ubj`, the JSON `ModelCard` sidecar, and the
   train/test CSVs.

Run:

```bash
make train-account_takeover       # 5000 samples → ato_reasoner/scorer/models/ato-v1.{ubj,json,train.csv,test.csv}
make eval-model-account_takeover  # Re-evaluate on a fresh-seed test set; exits non-zero if test_auc < 0.85
```

### Model card and integrity

The `ModelCard` (in `ato_reasoner/scorer/model_card.py`) is the persisted
contract between training and runtime. It records the band thresholds the
model was trained against and the band distribution on the test set. The
committed card `ato-v1.json` keeps the band key names it was written with,
as a training record (DR-30). At load time, `AtoScorer.__init__` validates
two integrity properties:

- `feature_names` in the card matches `FEATURE_NAMES` in
  `ato_reasoner/scorer/scorer.py`. Mismatch: `ValueError` (catches schema
  drift between training and inference).
- `artifact_sha256` in the card matches the binary on disk. Mismatch:
  `ValueError` (catches post-write tampering).

A legacy artifact without a sidecar still loads with a warning, so the change
is backward-compatible with cached test fixtures. The scorer's version
(`ReasonerContext.model_version`) and the artifact's SHA-256
(`model_artifact_sha256`) travel in every submission (DR-17).

---

## Decisions and tradeoffs

The MVP is deliberately scoped as a *reference architecture demonstration*,
not a production fraud detector. Every choice below reflects that scope.

- **Synthetic training data over real-data integration.** In scope is the
  *architecture pattern*: data, split, train, evaluate, version, deploy,
  integrity-check at load. The data-pipeline depth (real RBA ingestion,
  distribution fitting, validation) is a separate body of work scoped as a
  polish-phase follow-up in [Scenarios](scenarios.md). See DR-10.

- **Fixed band thresholds (0.20 / 0.95), registered as bypass rules, over
  per-deployment calibration.** Keeps the reference implementation
  reproducible and the band distribution interpretable across runs. The
  thresholds are ATO's registration data: a decision records the rule it
  cited and the registration version it was made under, and replay
  re-checks the rule. The model card records the thresholds a model was
  trained against and the band distribution they produce on its test set,
  so a reviewer can validate the choice without rerunning the pipeline; the
  committed `ato-v1.json` predates the widened cutoff and records an upper
  band of 0.85. Threshold tuning is a production concern
  after the MVP. The 0.95 upper cutoff (rather than the original 0.85)
  reflects the binary classifier's bimodal output distribution: moderately
  confident attacks (0.85 to 0.95) reach the policy gate for adjudication
  rather than being blocked outright. See DR-25 for the calibration record
  and DR-30 for the bypass rules.

- **Heuristic labels over hand-labeled data.** A triage-classifier label,
  not a fraud-detection label. The scorer's job is sorting logins into
  confidence bands so ambiguous ones are gated. See DR-10.

- **XGBoost over a neural network.** TreeSHAP gives per-event attribution
  with no separate explainability layer; CPU inference fits the under-10 ms
  latency budget; the artifact is under 1 MB, which fits the
  commit-the-baseline decision. A neural network would need a separate
  explainability tool, a GPU for low-latency inference, and a multi-MB
  artifact.

- **Sliding-window features in Redis over a feature store.** Redis is
  already ATO's infrastructure for ingestion; a managed feature store for the
  reference implementation adds infrastructure that does not pay for itself
  at MVP scope. A production deployment would likely replace it with Feast,
  Tecton, or a custom Kafka-backed store; the feature contract in
  `ato_reasoner/feature_service.py` is small enough to swap.

- **Commit the model artifact (and train/test CSVs).** Under 1 MB in total;
  the cost is a binary in git. The benefit is that a fresh checkout runs
  without a training step. A production system would use a model registry;
  for an MVP demonstration the simpler path wins.

- **Card-aware loading is mandatory when a card exists; a legacy load works
  without one.** Backward compatibility for cached test fixtures that
  predate the card. New artifacts always carry a card; old ones load with a
  warning.

---

## Cross-references

- **[Reasoners](../index.md)**: the abstract reasoner typology and required
  information.
- **[Submission contract](../reasoner-handoff.md)**: the wire contract and
  the [new-reasoner checklist](../reasoner-handoff.md#new-reasoner-checklist).
- **[Data Model](data.md)**: ATO's event, features, score, submission, and
  bundle.
- **[Scenarios](scenarios.md)**: scenario-generator calibration provenance
  and the polish-phase RBA-calibration plan.
- **[Policy Gate](../../gates/policy/index.md)**: the RAG policy gate ATO
  binds.
- **[Identity](../../identity.md)**: how the ledger resolves ATO's account.
- **DR-10**: hybrid data strategy.
- **DR-12** and **DR-32**: `entity_id` as the subject; the identity contract.
- **DR-17**: model-artifact fingerprint approach.
- **DR-25**: band calibration.
- **DR-26**: reasoner packaging and discovery (workspace member, entry
  points).
- **DR-29** to **DR-31**: gate sets, the record and gate modes, the wire
  submission and HTTP surface.

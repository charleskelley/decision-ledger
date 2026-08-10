# Reasoner

A **reasoner** is the component that owns observation, feature computation,
and risk scoring for a specific decision domain. It produces typed
`Observation` records and submits them to the DecisionLedger framework, which
takes over for retrieval, gate evaluation, deterministic enforcement, and
audit-bundle construction.

This document covers the **abstract** picture: what a reasoner can be, what
shapes it can take, and what information it must provide or have available
to hook into the framework. For the contract specification (the typed
boundary), see [`reasoner-handoff.md`](reasoner-handoff.md). For the
concrete reference implementation, see [`account-takeover.md`](account-takeover/account-takeover.md).

---

## What a reasoner is — and is not

A reasoner produces **observations**, not decisions. The framework owns the
final action. This is the load-bearing separation that makes governed
decisioning possible:

- The reasoner can be probabilistic, opaque, or vendor-supplied.
- The framework's enforcement is deterministic and auditable.
- The Decision Bundle records both — the reasoner's evidence and the
  framework's deterministic resolution of it — making every decision
  replayable.

A reasoner that produces a final action directly is not a reasoner under
this design; it's a complete decision system. This framework does not host
that pattern.

---

## Reasoner shapes

The framework imposes only the typed `Observation` contract. The reasoning
mechanism inside is plug-and-play.

| Shape | Examples | When it fits |
|---|---|---|
| **ML-only** | XGBoost, LightGBM, scikit-learn classifier, neural network | Mature labeled data; bounded output schema; strict latency budget |
| **LLM-only** | Direct LLM classification with structured output | Sparse labeled data; broad domain coverage needed; latency tolerable |
| **Rule-based** | Decision tree, expert system, deterministic feature thresholds | Hard regulatory requirements; explainability dominates; data scarce |
| **Hybrid (ML + LLM)** | Fast ML triage on confident events; LLM gate on ambiguous ones | Latency budget *and* coverage of edge cases needed |
| **Hybrid (rule + ML)** | Rule-based pre-filter + ML scoring | Regulatory hard constraints; ML refines within them |
| **Human-in-the-loop** | Feature service + analyst review | Low volume, very high stakes |

The ATO reasoner uses the ML+LLM hybrid pattern — fast XGBoost for
high-confidence routing, LLM policy gate for ambiguous cases. See
[`account-takeover.md`](account-takeover/account-takeover.md) for the concrete
architecture.

A reasoner can compose multiple shapes inside its own boundary; the
framework only sees the resulting `Observation`.

---

## What a reasoner must provide

The framework's intake (`validate_observation` in
`core/observation/observation.py`) checks for these fields on every
submitted observation. The reasoner's job is to produce them; the
framework's job is to refuse the submission if any are missing.

### Identity

- `event_id: str` — unique per event.
- `entity_id: UUID` — framework subject identity (typically derived
  deterministically from a domain business key via UUID5).
- `entity_type: str` — domain classification (`"account"`, `"transaction"`,
  `"document"`, etc.).
- `timestamp: datetime` — when the event occurred (UTC).

### Reasoning evidence — `ReasonerContext`

A typed record of what the reasoning component computed. Stored verbatim
in the Decision Bundle so a decision can be reconstructed without calling
back to the reasoner.

Required:

- `reasoner_id`, `reasoner_name`, `model_version` — identity of the
  reasoning artifact.
- `inference_latency_ms` — operational telemetry.
- `label_type`, `label_name`, `label_value` — what the reasoner produced
  (numerical risk score, categorical label, etc.).
- `feature_set: dict` — every feature value the reasoner consumed at
  inference time. This is what makes replay self-contained.

Optional but strongly recommended:

- `attribution: AttributionSummary` — observation-level SHAP, attention
  weights, or rule-trace explaining *this specific* decision (not just
  what the model tends to do globally). Required for meaningful audit
  quality; the framework accepts its absence but with reduced
  explainability.

### Gate dispatch envelope — `GateContext`

Required on **every** observation, including fast-path. The framework uses
this to:

- Select the appropriate gate (`gate_id`).
- Apply retrieval filters (`gate_config["jurisdictions"]`,
  `gate_config["risk_tier"]`).
- Render the LLM prompt (`gate_config["template_id"]`,
  `gate_config["template_vars"]`).

`GateContext` is required on fast-path observations to enable shadow
evaluation — the framework can retrospectively run the gate offline against
a fast-path bundle to compare scorer and gate verdicts on the same event.
Skipping this on fast-path forfeits that capability.

### Routing decision — `route`

The reasoner produces a `GateRoute` value:

- `FAST_PATH_ALLOW` — high confidence the event is benign; the gate is
  bypassed, enforcement applies a permissive default.
- `FAST_PATH_BLOCK` — high confidence the event is malicious;
  enforcement applies a blocking default.
- `ROUTE_TO_GATE` — ambiguous; the framework runs retrieval + gate.

A reasoner that uses ML scores typically maps confidence bands to routes
(e.g., score < 0.20 → ALLOW, > 0.95 → BLOCK, otherwise GATE). A
rule-based reasoner might route directly. An LLM-only reasoner might route
everything to GATE since the LLM is already its scoring mechanism.

### Fast-path rationale — `fast_path_rationale`

Required when `route` is `FAST_PATH_*`. Human-readable string explaining
why the gate was bypassed (e.g., `"risk_score=0.97 > 0.95 → FAST_PATH_BLOCK"`).
This is the audit-trail substitute for the gate's rationale on bypassed
events.

---

## What a reasoner must have available to hook in

### Feature computation

A way to compute the features the reasoner consumes. For batch domains
this might be precomputed; for streaming domains (the ATO reference) this
is done online from the inbound event plus prior state.

The framework does not prescribe a feature store — `reasoner/account_takeover/feature_service.py` in the
ATO reference uses Redis sliding-windows; another domain might use
DataFrames, SQL, or a managed feature store. What matters is that
`feature_set` in `ReasonerContext` is populated with everything the model
consumed at inference time.

### Confidence or score primitive

The reasoner needs to produce a confidence/score signal. This drives
routing (`GateRoute`), populates `label_value`, and feeds the
fast-path-rationale string. ML reasoners output probabilities natively;
rule-based reasoners can map rule outcomes to bands.

### Explainability surface

For meaningful `attribution`, the reasoner needs an introspection
mechanism: TreeSHAP for tree models, integrated gradients for neural
networks, attention weights for transformers, rule-trace for rule
engines, or qualitative narrative for LLM reasoners. The framework records
whatever shape the reasoner can produce; quality of the audit signal
scales with the quality of attribution.

### Operating-mode awareness

The reasoner must know whether it's operating in **governed mode** (full
gate participation; default for the reference implementation) or
**shadow/opt-out mode** (registration-time governance decision; rare). See
[`reasoner-handoff.md`](reasoner-handoff.md) for the full operating-mode
contract.

### Assembler

The handoff happens at one point: when the reasoner calls
`build_observation()` (or its domain-specific equivalent) and submits the
result to the framework pipeline. Everything before that call is the
reasoner's responsibility; everything after is the framework's.

The ATO reference's assembler is at
`reasoner/account_takeover/assembler.py` — that's the canonical pattern
for any domain reasoner.

---

## Anti-patterns

- **Reasoners that produce decisions directly.** The framework owns
  decisions. A reasoner outputs evidence (`ReasonerContext`) and routing
  (`GateRoute`); enforcement decides.
- **Reasoners that omit `GateContext` on fast-path.** Forbidden — shadow
  evaluation needs `GateContext` populated on every observation
  regardless of routing.
- **Reasoners that mutate `ReasonerContext` after assembly.** Pydantic
  models in `core/observation/` are `frozen=True` by design. Mutation
  defeats replay.
- **Reasoners that bypass `validate_observation`.** The framework's
  intake validates the contract; the reasoner doesn't get to
  self-certify.
- **Reasoners with no attribution.** The framework accepts it (some
  vendor models can't expose attribution), but a reasoner with no
  `AttributionSummary` produces decisions that can't be explained — a
  governance liability the framework can't fix.

---

## Cross-references

- **[`reasoner-handoff.md`](reasoner-handoff.md)** — the typed contract
  at the framework boundary; field-by-field specification.
- **[`account-takeover.md`](account-takeover/account-takeover.md)** — the ATO reasoner
  reference architecture, including its hybrid fast/gate routing,
  feature pipeline, and decisions & tradeoffs.
- **[`scenarios.md`](account-takeover/scenarios.md)** — calibration provenance for the
  ATO reasoner's training-data foundations.
- **DR-11** — the framework / reasoner package boundary.
- **DR-14** — `Observation` as the domain-assembled output.

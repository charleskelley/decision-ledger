# Pipeline

The runtime path of one decision: what the reasoner does before it submits,
what the ledger does with the submission, the latency budget, and how each
failure resolves.

The path has two halves. Everything up to and including the `Submission` is
the reasoner's: its ingestion, features, model, and assembler, in its own
package and, usually, its own process. Everything after is the ledger's,
entered through `app/submit.py` (`decide()`, and `submit()`, which also writes
the bundle). A reasoner reaches it over HTTP (`POST /api/v1/decisions`,
`app.client.HttpClient`) or in process (`app.client.LocalClient`); both run the
same operation, `app.api.decisions.create_decision`. A different reasoner
replaces the first half and reuses the second unchanged (see
[Submission contract](reasoners/reasoner-handoff.md)).

---

## Record or gate

Every submission names its mode (DR-30):

- **`record`**: the reasoner decided. The submission carries its
  `proposed_action` and a `bypass` citation of one of its registered bypass
  rules. The ledger checks the citation, runs the mandatory gates only, and
  records the advisory gates as skipped.
- **`gate`**: the reasoner asks the ledger to decide. The `proposed_action` is
  optional (a floor, never a ceiling). The ledger runs the mandatory gates,
  then the advisory ones.

A reasoner picks the mode itself, usually with
`core.submission.matching_bypass_rule`, which returns the strictest
registered rule that holds on the evidence. ATO's assembler records under
that rule or asks for the gate. ATO's two rules are its own, registered in
`ato_reasoner/registry.py`: `allow-band` (`label_value < 0.20` records ALLOW)
and `block-band` (`label_value > 0.95` records BLOCK). The framework has no
thresholds.

---

## Decision Path Overview

```
── Reasoner (ATO as the example; ato_reasoner) ───────────────────────────
POST /api/v1/ato/decisions on ATO's own service (port 8001)
    ↓
Online features         sliding-window velocity, novelty, geo, device (Redis, ATO-owned)
    ↓
Scorer                  XGBoost risk score + top-k SHAP signals
    ↓
Assembler               build_submission() → Submission (mode record or gate)
    ↓  LocalClient (in process) or HttpClient (POST /api/v1/decisions)
── Ledger (app/submit.py) ────────────────────────────────────────────────
1. Intake validation    registration, citation, native subject, gate contexts,
                        each gate's config_model and authorization
2. Identity             the active resolver, or the passthrough (unresolved)
3. Gate set             registration bindings + deployment mandates
4. Mandatory gates      concurrently, on every decision
5. Advisory gates       concurrently, gate mode only; skipped once a mandatory gate blocks
6. Gate enforcement     gate-level tiers per gate (a failed gate holds)
7. Decision rules       Tier 3 on the advisory side, bypass floors, most restrictive
8. Write                version-2 bundle, stamped with registration_version
    ↓
Receipt                 decision_id, decision_action, each gate's outcome
```

---

## The Reasoner's Half

The reasoner owns everything before the submission. The framework neither
imports nor runs this code; this section describes ATO, the reference
reasoner, as an example.

**Ingestion (ATO).** ATO's service receives a `LoginEvent` at
`POST /api/v1/ato/decisions` and validates it strictly (422 on failure). Its
package also ships a Redis Streams consumer (`ato_reasoner/ingestion/`) that
reads `ato:login-events`, validates each message against `LoginEvent`,
enforces a bounded-lateness window (one hour by default), deduplicates by a
SHA-256 of the `event_id` (Redis `SET NX` with a 24-hour TTL), and routes every
failure to `ato:login-events:dlq` with a reason of `schema_error`, `lateness`,
or `duplicate`. The scenario generator publishes to that stream
(`make scenario-account_takeover`), and `python -m ato_reasoner consume`
decides each accepted event through `run_ato_decision`, the pipeline the
service's route runs. A decision the ledger refuses (a 422 or 413 about
that submission) is dead-lettered as `ledger_refused`; any other failure of a
decision is dead-lettered as `pipeline_error` and stops the consumer, whose
next run decides the rest of that batch first.

**Features (ATO).** `ato_reasoner/feature_service.py` reads the account's
history from Redis in one pipelined round trip and computes an
`AtoFeatureVector`, keyed by `account_id`:

| Feature | Description |
|---------|-------------|
| `velocity_1min`, `velocity_5min`, `velocity_60min`, `velocity_1440min` | Event count for the account in the window |
| `ip_novelty`, `device_novelty`, `geo_novelty` | Novelty of the IP, device fingerprint, and country against the account's history |
| `impossible_travel`, `travel_speed_kmh` | Implied speed from the last location |
| `device_consistency_score`, `user_agent_consistency` | Consistency with the account's devices and user agents |
| `sparse_history` | Too little history for a confident decision (ATO's novel-entity signal) |

**Scorer (ATO).** XGBoost inference over the vector produces a
`ScorerOutput` (`risk_score` in [0, 1], `top_signals` as SHAP contributions,
`scorer_version`, `scorer_artifact_sha256`, `inference_latency_ms`). It is an
ATO type and never reaches the ledger as a type.

**Assembler (ATO).** `ato_reasoner/assembler.py:build_submission` translates
the event, features, and score into the framework's `Submission`: the
`ReasonerContext` (the score as `label_value`, the complete feature snapshot
as `feature_set`, the SHAP attribution), one `GateContext` for the RAG policy
gate (template id, template variables, retrieval query, jurisdiction and
risk-tier filters; the corpus is the registration's binding, not the
submission's), the native subject
`(ato-account, account_id)`, the event as opaque `raw_event`, and the mode.
`run_ato_decision` then submits through its `LedgerClient` and returns the
receipt with ATO's own step timings (`features_ms`, `scorer_ms`, `ledger_ms`).

---

## The Ledger's Half

The order below is `app/submit.py`'s. `decide()` runs steps 1 to 7 and returns
the bundle unwritten (the batch route writes many in one round trip);
`submit()` is `decide()` plus step 8.

### 1. Intake validation

Nothing is persisted and no gate runs until the submission passes intake.

- **Shape.** The body must parse as a `Submission`
  (`Submission.model_validate_json`; strict, unknown fields rejected,
  non-finite numbers, NUL characters, and invalid Unicode rejected anywhere).
  A record must carry its citation and proposal; a gate request must not cite
  a rule. Failure: 422 `SUBMISSION_INVALID`.
- **Caller.** A reasoner key may submit only for its own `reasoner_id`
  (403 `REASONER_MISMATCH`); the operator key submits nothing
  (403 `REASONER_KEY_REQUIRED`).
- **Registration.** The reasoner must be registered
  (422 `REASONER_NOT_REGISTERED`). `core.submission.validate_submission` then
  checks, in this order, the citation (`BYPASS_RULE_UNREGISTERED`,
  `ACTION_NOT_RECORDABLE`, `BYPASS_ACTION_MISMATCH`,
  `BYPASS_RULE_VERSION_MISMATCH`), a native subject in a namespace the
  registration declares against the submission's `entity_id` and
  `entity_type` (`NATIVE_SUBJECT_MISMATCH`), and the gate contexts: only gates
  the registration binds (`GATE_NOT_BOUND`), one for every bound gate, on
  records too (`GATE_CONTEXT_MISSING`), and at least one gate for a gate
  request (`NO_GATES`).
- **Gate configuration.** Every composed gate in the decision's gate set
  validates its context: its `config_model` (`GATE_CONFIG_INVALID`) and, when
  the built gate is a `ConfigAuthorizer`, whether this reasoner may send that
  configuration (`GATE_CONFIG_UNAUTHORIZED`). This runs in both modes, so a
  recorded decision always carries contexts its advisory gates could evaluate
  later (shadow evaluation).

Each refusal is an RFC 9457 problem document with its stable code: 401
`UNAUTHENTICATED` for a missing or unknown key (before anything else), 413
`BODY_TOO_LARGE` for a body over 1 MiB (before it is parsed), 403 for the
caller checks, 422 for the rest (see [Interfaces](core.md#error-types)).

### 2. Identity

The ledger establishes the decision's subject before any gate runs (DR-32).
`IdentityService.identify` asks the deployment's active resolver to map the
submission's native subject; the resolver is synchronous and runs in a worker
thread under a 50 ms budget (`DEFAULT_IDENTITY_BUDGET_MS`). Its answer is used
only if it is a usable match. Otherwise (no resolver, no native subject, no
match, an error, a timeout, an unusable answer) the subject is the
**passthrough**: a native subject in a declared namespace is that
declaration's derivation, and any other subject is the submission's own
`entity_id` and `entity_type`; either way it is recorded `unresolved`, with
the reason. Identity never refuses a decision. The result is the bundle's
`identity`, every gate's `GateRequest.identity`, and the subject the decision
is filed under. See [Identity](identity.md#the-identity-step).

### 3. Gate set

`core.submission.effective_gate_set` is the registration's bindings, in
order, with any gate the deployment mandates
(`DECISIONLEDGER_MANDATORY_GATES`) upgraded to mandatory, plus every other
mandated gate as a mandatory binding. A mandated gate the reasoner does not
bind runs with an empty configuration, which startup checks it accepts.

### 4. Mandatory gates

The mandatory gates run concurrently, on every decision, recorded or gated.
Each gets a `GateRequest` (the decision id, the submission, its own
`GateContext`, the resolved identity, and its budget) and none sees another's
output (DR-29). `app.gates.invoke_gate` runs each one under its budget and
fails it closed (step 6).

### 5. Advisory gates

On a gated decision, the advisory gates run next, concurrently, unless a
mandatory gate's enforced action is already BLOCK: BLOCK absorbs in the
combinator, so they are then recorded `skipped` with the reason. On a recorded
decision every advisory gate is recorded `skipped`. The reasoner's own
proposal never skips them: a proposed BLOCK on a gated decision still runs the
advisory gates.

For the RAG policy gate (`gate_id` `policy`, bound advisory by ATO and by the
example reasoner), evaluation is the gate's own business: one query per
corpus the reasoner's binding names (ATO: `ato-policy`; the example:
`example-policy`), each through the retrieval provider that holds it — the
reference provider's hybrid retrieval is pgvector dense search and
Elasticsearch BM25, filtered by `corpus_id` and jurisdictions in both stores
and by risk tier in pgvector, fused by Reciprocal Rank Fusion, then
reranked by a cross-encoder, falling back to the fused order when reranking
exceeds its timeout — then rendering the reasoner's prompt template with
the passages in precedence order, one structured-output LLM call, and
returning a `GateOutput` whose evidence is the rendered passages with their
jurisdictions and content hashes. See [Policy Gate](gates/policy/index.md).

### 6. Gate-level enforcement

`app.enforcement.resolver.resolve_gate_output` applies the gate-level tiers
to every gate that ran or failed. The first that fires decides that gate's
enforced action:

| Tier | Condition | Result |
|------|-----------|--------|
| 1. Schema failure | No output (the gate failed closed) or `verdict is None` | HOLD |
| 2. Adversarial probe | `adversarial_probe_detected` in `required_controls`; skipped for a deterministic gate | BLOCK |
| 4. Low confidence | `confidence < 0.60` and the most conservative permitted action is HOLD or BLOCK; skipped for a deterministic gate | HOLD |
| 5. Jurisdiction conflict | The gate flags a conflict (`jurisdiction_conflict` control, or a conflict-worded escalation), or its evidence spans two or more of the registration's `jurisdiction_conflict_groups`; skipped when the evidence carries no jurisdictions | HOLD |
| 6. Default | No tier fired | The most conservative permitted action (HOLD when none is permitted) |

A gate fails closed (`app.gates.invoke_gate`) when it is not composed, raises,
is still running at its deadline or returns after it, or returns anything
other than a plain `GateOutput` for itself, with its declared `deterministic`
flag, that the ledger can store and reload as returned. It is recorded
`failed` with the reason, and Tier 1 holds it.

### 7. Decision-level rules and the combinator

`resolve_decision` applies the decision-level rules once, then the fixed
combinator (`core.gate.combinator.most_restrictive`, over
ALLOW < CHALLENGE < HOLD < BLOCK):

```
final = most_restrictive(proposed, every mandatory gate's action, the advisory side, bypass floors)
```

- **Tier 3, novel entity** (gated decisions only). When the registration's
  `novel_entity_feature` is truthy in the `feature_set`, the advisory gates'
  verdicts are held for review: the advisory side becomes HOLD, or BLOCK when
  an advisory gate's own Tier 2 fired. It never lowers a mandatory gate's
  action or the proposal, and it is not applied when a mandatory gate already
  blocks. On a gated decision with no advisory gate, it is a HOLD floor. ATO's
  novel-entity feature is `sparse_history`.
- **Bypass rules** (recorded decisions only). The ledger evaluates the cited
  rule on the submission's evidence: when it does not hold, or cannot be
  evaluated, `bypass_rule_not_satisfied` puts a HOLD floor under the decision.
  It evaluates every other registered rule too: when one that holds justifies
  a stricter action, `bypass_rule_superseded` floors the decision at that
  action. Records skip Tier 3. All the checks are snapshotted in the bundle
  (`bypass_check`, `other_bypass_checks`, `bypass_rule_holds`).

The combinator is associative and commutative, so gate order and parallelism
cannot change the result, and no gate can relax another. Every tier and rule
is logged in the gate's or the decision's `override_log`, ending with the
combinator line.

**Invariant:** enforcement is pure, rule-based, and makes no gate or LLM
call. Given the same stored gate outputs, submission, and registration, it
always produces the same action; that is what replay relies on.

### 8. Write and receipt

`submit()` writes the version-2 `DecisionBundle` (`app/audit/store.py`): the
submission verbatim, the resolved identity, one `GateRecord` per gate
(`ran`, `skipped`, or `failed`, with its output and gate-level enforcement),
the decision action, the decision-level rule and log, the bypass checks, the
latency breakdown, and the `registration_version` of the registration intake
used (DR-31). Writing it also opens any relationship the identity asserts in
the membership projection. The write returns a `DecisionReceipt`
(`POST /api/v1/decisions` answers 201 with a `Location` header): the decision
id, the resolved `entity_id` and `entity_type`, the mode, `decision_action`,
the decision-level and credited rules, the cited rule and whether it held, the
registration version, and each gate's status and action. The full bundle is
at `GET /api/v1/decisions/{decision_id}`.

**Replay** (`POST /api/v1/decisions/{decision_id}/replay`) re-runs steps 6 and
7 on the stored gate outputs, with the gates' recorded statuses and the
snapshotted bypass rules, under the snapshot of the registration version the
bundle cites. No gate is re-invoked and no corpus is read. The result is
written to `replay_logs`.

**Verify** (`POST /api/v1/decisions/{decision_id}/verify`, DR-34) is a
different question: whether what the decision relied on is still provable.
For each evidence reference of each gate that ran, the ledger says whether
its evidence escrow holds the artifact the reference names (the gate
escrowed it while deciding, in step 4 or 5), and asks the gate, when it verifies
its own references, how the reference stands: against its own record, or
with `?live=true` against the source as it is now (`intact`,
`source_changed`, `source_unavailable`, `not_verifiable`). Nothing is
decided again and nothing is written.

### After the decision: resolution

CHALLENGE and HOLD are non-terminal. The bundle is immutable; a human
resolution is appended to `decision_resolution_attempts`
(`POST /api/v1/decisions/{decision_id}/resolutions`, operator key), and the
realized action is derived at read time by folding the attempt chain (DR-18).

---

## Latency Budget

The repository records no latency measurements: the per-component numbers
below are DR-5's design targets, and DR-5 is still Proposed, awaiting measured
P50 and P95 numbers. MVP scope excludes production SLA hardening. The
budgets the code enforces are these:

| Budget | Value | Where |
|--------|-------|-------|
| Identity resolver call | 50 ms (default) | `DEFAULT_IDENTITY_BUDGET_MS`, `app/identity/resolvers.py` |
| Each gate invocation | 45 s (default) | `DEFAULT_GATE_BUDGET_MS`, `app/submit.py` |
| RAG gate model call | 30 s per request (adapter default) | `policy_rag_gate/llm/` |
| RAG gate reranking | 200 ms, then RRF-only results | `PolicyRetriever(rerank_timeout_ms=...)` |
| `HttpClient` waiting for a decision | 120 s | `DEFAULT_TIMEOUT_S`, `app/client.py` |

Neither the identity budget nor the gate budget is an environment setting.

DR-5's targets:

| Component | Target | Owner |
|-----------|--------|-------|
| Ingestion | <5 ms | Reasoner (ATO) |
| Feature computation | <15 ms | Reasoner (ATO) |
| Scoring | <10 ms | Reasoner (ATO) |
| Retrieval and reranking | <150 ms | RAG policy gate |
| LLM inference | <2000 ms | RAG policy gate |
| Enforcement | <5 ms | Ledger |
| **Total, gated through the RAG gate** | **<2200 ms** | |
| **Total, scorer only (a recorded decision, no gate model call)** | **<30 ms** | |

A recorded decision runs no advisory gate, so its latency is the reasoner's
own steps plus the identity step, any mandatory gates, enforcement, and the
write. Each bundle's `latency_breakdown` holds the ledger's own steps
(`identity_ms`, `gate_ms`, `enforcement_ms`); a reasoner reports its own
timings itself (ATO returns them with the receipt).

---

## Fallback Behavior

The ledger fails closed: a failure holds the decision or raises its floor; it
never relaxes it or drops it.

| Failure | Result |
|---------|--------|
| A gate raises, times out, returns an unusable output, or is not composed | That gate is recorded `failed`; Tier 1 makes its enforced action HOLD |
| A gate returns no verdict (`verdict is None`; for the RAG gate: no retrieval query, a render error, an LLM error, a schema refusal) | Tier 1: HOLD |
| The identity resolver errs, times out, does not match, or answers unusably | The passthrough subject, recorded `unresolved` with the reason; the decision proceeds |
| A record's cited bypass rule does not hold, or cannot be evaluated | `bypass_rule_not_satisfied`: HOLD floor |
| A stricter registered rule also holds on a record's evidence | `bypass_rule_superseded`: that rule's action as the floor |
| The submission breaks its registration or a gate's configuration | Refused at intake (422 with a stable code); nothing is written |
| A deployment composes a broken plugin, or mandates a gate it does not compose | Startup fails (`GatePluginError`, `ReasonerPluginError`, `ResolverPluginError`, `RegistrationError`) |

Inside the RAG policy gate, a reranker that exceeds its timeout degrades to the
RRF-only order rather than failing: retrieval without reranking is still
meaningful retrieval, and the gate records which path it took in its evidence
snapshot. A failure in the reasoner's own half (ATO's feature store or scorer)
is the reasoner's to handle; nothing reaches the ledger.
